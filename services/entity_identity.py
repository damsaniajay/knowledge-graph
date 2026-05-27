"""
Resolve or assign entity base_ids on upload (no IDs required in user files).

- LLM matches uploads to existing Neo4j entities when content is a delta/version.
- Heuristic fallback: title/name matching, then generated IDs (US1, TC-…).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import config
from services import graph_service as gs
from services.llm_client import LLMError, chat_json

logger = logging.getLogger(__name__)

_MATCH_SYSTEM = (
    "You are a knowledge-graph identity resolver. Given an uploaded document and "
    "existing entities of the same type, decide if the upload is a NEW entity or a "
    "VERSION UPDATE (delta) of an existing one. Reuse the existing base_id when it is "
    "the same product entity with changed content. Return JSON only."
)

_LINK_TESTCASE_SYSTEM = (
    "You are a knowledge-graph linker. Given a test case and a free-form linked_to hint, "
    "choose which EXISTING entity (Feature, UserStory, or APIEndpoint) it is referring to. "
    "Return JSON only.\n\n"
    "Rules:\n"
    "- If the linked_to hint already exactly matches an entity from the candidates, pick it.\n"
    "- Otherwise, pick the closest match based on test case title/steps/content and entity names/paths.\n"
    "- If no good match exists, return match_found=false.\n"
    "- target_linked_to must be:\n"
    "  - Feature: the Feature base_id\n"
    "  - UserStory: the UserStory base_id\n"
    "  - APIEndpoint: the APIEndpoint base_id in the form METHOD:/path\n"
)


def _slug(value: str, max_len: int = 32) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip()).strip("-").upper()
    return (s[:max_len] if s else "ENTITY")


def _all_stories() -> list[dict]:
    try:
        return gs.get_all_user_stories()
    except Exception as e:
        logger.warning("Could not load stories from Neo4j: %s", e)
        return []


def _all_test_cases() -> list[dict]:
    try:
        return gs.get_all_test_cases()
    except Exception as e:
        logger.warning("Could not load test cases from Neo4j: %s", e)
        return []


def _next_us_id() -> str:
    max_n = 0
    for s in _all_stories():
        m = re.fullmatch(r"US(\d+)", s.get("base_id", ""), re.IGNORECASE)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"US{max_n + 1}"


def _next_tc_id(title: str) -> str:
    base = f"TC-{_slug(title, 24)}"
    existing = {t.get("base_id") for t in _all_test_cases()}
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _story_candidates() -> list[dict]:
    out = []
    for s in _all_stories():
        content = (s.get("content") or "")[:500]
        out.append({
            "base_id": s["base_id"],
            "title": s.get("title", ""),
            "content_preview": content,
            "version": s.get("version"),
        })
    return out


def _llm_match_story(item: dict, candidates: list[dict], hint_id: str | None) -> dict | None:
    if not candidates:
        return None
    user = f"""Uploaded user story (no id in file):
{{
  "title": {item.get("title", "")!r},
  "content": {(item.get("content") or "")[:4000]!r},
  "hint_id": {hint_id!r}
}}

Existing stories in the graph:
{json_candidates(candidates)}

Return JSON:
{{
  "match_base_id": "US1 or null if this is a genuinely new story",
  "is_version_update": true/false,
  "confidence": 0.0-1.0,
  "delta_summary": "what changed vs matched story, or why new"
}}
"""
    result = chat_json(_MATCH_SYSTEM, user)
    bid = result.get("match_base_id")
    if bid in (None, "null", ""):
        return None
    bid = str(bid).strip()
    if not any(c["base_id"] == bid for c in candidates):
        return None
    if float(result.get("confidence", 0)) < 0.5:
        return None
    return {
        "base_id": bid,
        "is_version_update": bool(result.get("is_version_update", True)),
        "confidence": float(result.get("confidence", 0.8)),
        "delta_summary": str(result.get("delta_summary", "")),
        "source": "llm",
    }


def json_candidates(candidates: list[dict]) -> str:
    import json
    return json.dumps(candidates, indent=2)


def _heuristic_match_story(item: dict, hint_id: str | None) -> dict | None:
    title = (item.get("title") or "").strip().lower()
    if hint_id:
        try:
            existing = gs.get_user_story(hint_id)
        except Exception:
            existing = None
        if existing:
            return {
                "base_id": hint_id,
                "is_version_update": True,
                "confidence": 1.0,
                "delta_summary": "Explicit story_id in upload",
                "source": "hint_id",
            }
    for s in _all_stories():
        if (s.get("title") or "").strip().lower() == title and title:
            return {
                "base_id": s["base_id"],
                "is_version_update": True,
                "confidence": 0.85,
                "delta_summary": "Same title as existing story",
                "source": "title_match",
            }
    return None


def resolve_user_story(item: dict) -> tuple[dict, dict]:
    hint_id = (item.get("story_id") or "").strip() or None
    meta: dict[str, Any] = {"entity_type": "user_story"}

    candidates = _story_candidates()
    match = None
    if config.use_llm_entity_match() and not config.UPLOAD_FAST and candidates:
        try:
            match = _llm_match_story(item, candidates, hint_id)
        except LLMError as e:
            logger.warning("LLM story identity match failed: %s", e)
            meta["identity_warning"] = str(e)

    if not match:
        match = _heuristic_match_story(item, hint_id)

    if match:
        item["story_id"] = match["base_id"]
        meta.update({
            "assigned_id": match["base_id"],
            "is_version_update": match["is_version_update"],
            "identity_source": match["source"],
            "delta_summary": match.get("delta_summary", ""),
            "confidence": match.get("confidence"),
        })
    else:
        new_id = hint_id or _next_us_id()
        item["story_id"] = new_id
        meta.update({
            "assigned_id": new_id,
            "is_new_entity": True,
            "identity_source": "generated" if not hint_id else "hint_id",
        })
    return item, meta


def resolve_feature(item: dict) -> tuple[dict, dict]:
    hint_id = (item.get("feature_id") or "").strip() or None
    name = (item.get("name") or hint_id or "").strip()
    if not name:
        raise ValueError("Feature upload requires 'name'")
    item["name"] = name

    meta: dict[str, Any] = {"entity_type": "feature"}
    existing = None
    try:
        existing = gs.get_feature(hint_id) if hint_id else None
        if not existing:
            existing = gs.get_feature(name) or gs.get_feature_by_name(name)
    except Exception as e:
        logger.warning("Could not look up feature in Neo4j: %s", e)

    if existing:
        item["feature_id"] = existing["base_id"]
        meta.update({
            "assigned_id": existing["base_id"],
            "is_version_update": True,
            "identity_source": "name_match",
        })
    else:
        item["feature_id"] = hint_id or name
        meta.update({
            "assigned_id": item["feature_id"],
            "is_new_entity": True,
            "identity_source": "generated" if not hint_id else "hint_id",
        })
    return item, meta


def _resolve_linked_to(linked: str) -> str:
    linked = (linked or "").strip()
    if not linked:
        return linked
    try:
        if gs.get_user_story(linked):
            return linked
        if gs.get_feature(linked):
            return linked
        feat = gs.get_feature_by_name(linked)
        if feat:
            return feat["base_id"]
    except Exception as e:
        logger.warning("Could not resolve linked_to in Neo4j: %s", e)
    for s in _all_stories():
        if (s.get("title") or "").strip().lower() == linked.lower():
            return s["base_id"]
    return linked


def _tc_text_for_linking(item: dict) -> str:
    title = str(item.get("title") or "")
    expected = str(item.get("expected_result") or "")
    steps = item.get("steps") or []
    if isinstance(steps, list):
        steps_text = " ".join(map(str, steps))
    else:
        steps_text = str(steps)
    return f"{title}\n{expected}\n{steps_text}".strip()


def _top_by_score(candidates: list[dict], score_fn, limit: int = 10) -> list[dict]:
    scored = []
    for c in candidates:
        try:
            scored.append((score_fn(c), c))
        except Exception:
            continue
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:limit]]


def _llm_resolve_linked_to_for_test_case(item: dict, linked_to_input: str) -> dict | None:
    """
    Resolve a free-form `linked_to` (e.g. "Login" or "POST:/auth/login")
    into an existing entity base_id so the relationship mapper can attach
    the uploaded TestCase to the right Feature/UserStory/APIEndpoint.
    """
    linked_to_input = str(linked_to_input or "").strip()
    if not linked_to_input:
        return None

    tc_text = _tc_text_for_linking(item).lower()

    # Gather candidates from Neo4j.
    try:
        features = gs.get_all_features()
        stories = gs.get_all_user_stories()
        endpoints = gs.get_all_endpoints()
    except Exception as e:
        logger.warning("Could not load linking candidates from Neo4j: %s", e)
        return None

    def score_feature(f: dict) -> float:
        name = str(f.get("name") or "")
        base = str(f.get("base_id") or "")
        apis = " ".join(map(str, f.get("apis_used") or []))
        s = 0.0
        if linked_to_input.lower() == base.lower():
            s += 5
        if linked_to_input.lower() == name.lower():
            s += 4
        if linked_to_input.lower() in name.lower():
            s += 1.5
        if apis and apis.lower() in tc_text:
            s += 1.0
        if name and name.lower() in tc_text:
            s += 1.0
        return s

    def score_story(st: dict) -> float:
        title = str(st.get("title") or "")
        base = str(st.get("base_id") or "")
        content_preview = str(st.get("content") or "")[:800]
        s = 0.0
        if linked_to_input.lower() == base.lower():
            s += 5
        if linked_to_input.lower() == title.lower():
            s += 4
        if title and linked_to_input.lower() in title.lower():
            s += 1.5
        if title and title.lower() in tc_text:
            s += 1.0
        if content_preview and content_preview.lower() in tc_text:
            s += 0.5
        return s

    def score_endpoint(ep: dict) -> float:
        base = str(ep.get("base_id") or "")
        method = str(ep.get("method") or "")
        path = str(ep.get("path") or "")
        summary = str(ep.get("summary") or "")
        s = 0.0
        if linked_to_input.lower() == base.lower():
            s += 6
        if path and linked_to_input.lower() in path.lower():
            s += 2.0
        if method and linked_to_input.upper().startswith(method.upper()):
            s += 1.0
        if (method and method.lower() in tc_text) or (path and path.lower() in tc_text):
            s += 1.5
        if summary and summary.lower() in tc_text:
            s += 0.5
        return s

    top_features = _top_by_score(features, score_feature, limit=12)
    top_stories = _top_by_score(stories, score_story, limit=8)
    top_endpoints = _top_by_score(endpoints, score_endpoint, limit=12)

    # Truncate candidate fields to keep prompt small.
    def tcand_feature(f: dict) -> dict:
        return {
            "base_id": f.get("base_id"),
            "name": f.get("name"),
            "apis_used": (f.get("apis_used") or [])[:6],
        }

    def tcand_story(s: dict) -> dict:
        return {
            "base_id": s.get("base_id"),
            "title": s.get("title"),
            "content_preview": (s.get("content") or "")[:500],
        }

    def tcand_endpoint(e: dict) -> dict:
        return {
            "base_id": e.get("base_id"),
            "method": e.get("method"),
            "path": e.get("path"),
            "summary": (e.get("summary") or "")[:180],
        }

    candidates = {
        "features": [tcand_feature(f) for f in top_features if f.get("base_id")],
        "stories": [tcand_story(s) for s in top_stories if s.get("base_id")],
        "endpoints": [tcand_endpoint(e) for e in top_endpoints if e.get("base_id")],
    }

    import json

    user_prompt = f"""Test case (uploaded):
{{
  "title": {item.get("title", "")!r},
  "type": {item.get("type", "")!r},
  "linked_to_input": {linked_to_input!r},
  "steps": {item.get("steps", [])!r},
  "expected_result": {item.get("expected_result", "")!r}
}}

Existing entity candidates:
{json.dumps(candidates, indent=2)}

Return best match entity for test case.
"""

    try:
        result = chat_json(_LINK_TESTCASE_SYSTEM, user_prompt)
    except LLMError as e:
        logger.warning("LLM linked_to match failed: %s", e)
        return None

    if not isinstance(result, dict):
        return None
    if not result.get("match_found"):
        return None
    if float(result.get("confidence", 0)) < 0.55:
        return None
    target = str(result.get("target_linked_to") or "").strip()
    if not target:
        return None
    return result


def resolve_test_case(item: dict) -> tuple[dict, dict]:
    hint_id = (item.get("tc_id") or "").strip() or None
    title = (item.get("title") or "").strip()
    if not title:
        raise ValueError("Test case upload requires 'title'")

    linked = item.get("linked_to") or item.get("flow_id") or ""
    if linked:
        item["linked_to"] = _resolve_linked_to(str(linked))

    meta: dict[str, Any] = {"entity_type": "test_case"}
    matched = None
    if hint_id:
        try:
            existing = gs.get_test_case(hint_id)
            if existing:
                matched = existing
        except Exception as e:
            logger.warning("Could not look up test case in Neo4j: %s", e)

    if not matched:
        for tc in _all_test_cases():
            if (tc.get("title") or "").strip() == title and (tc.get("linked_to") or "") == (item.get("linked_to") or ""):
                matched = tc
                break

    # If linked_to still doesn't resolve to any existing entity, use LLM to map
    # it to the closest existing Feature/UserStory/APIEndpoint.
    if linked and not matched and config.use_llm_entity_match() and not config.UPLOAD_FAST:
        try:
            resolved = gs.resolve_entity(str(item.get("linked_to") or "").strip())
        except Exception:
            resolved = None
        if not resolved:
            llm = _llm_resolve_linked_to_for_test_case(item, item.get("linked_to") or "")
            if llm:
                item["linked_to"] = llm.get("target_linked_to")
                meta["linked_to_identity_source"] = "llm"
                meta["linked_to_confidence"] = llm.get("confidence")

    if matched:
        item["tc_id"] = matched["base_id"]
        meta.update({
            "assigned_id": matched["base_id"],
            "is_version_update": True,
            "identity_source": "title_link_match",
        })
    else:
        item["tc_id"] = hint_id or _next_tc_id(title)
        meta.update({
            "assigned_id": item["tc_id"],
            "is_new_entity": True,
            "identity_source": "generated" if not hint_id else "hint_id",
        })
    return item, meta


def resolve_api_endpoint(item: dict) -> tuple[dict, dict]:
    path = item.get("path", "")
    method = (item.get("method") or "GET").upper()
    base_id = f"{method}:{path}"
    meta = {
        "entity_type": "api_endpoint",
        "assigned_id": base_id,
        "identity_source": "method_path",
    }
    existing = gs.get_endpoint_by_path(path, method)
    if existing:
        meta["is_version_update"] = True
    else:
        meta["is_new_entity"] = True
    return item, meta


def resolve_item(entity_type: str, item: dict) -> tuple[dict, dict]:
    if entity_type == "user_story":
        return resolve_user_story(item)
    if entity_type == "feature":
        return resolve_feature(item)
    if entity_type == "test_case":
        return resolve_test_case(item)
    if entity_type == "api_endpoint":
        return resolve_api_endpoint(item)
    return item, {"entity_type": entity_type}


def resolve_upload_items(entity_type: str, items: list[dict]) -> tuple[list[dict], list[dict]]:
    resolved, meta_list = [], []
    for item in items:
        item2, meta = resolve_item(entity_type, item.copy())
        resolved.append(item2)
        meta_list.append(meta)
    return resolved, meta_list
