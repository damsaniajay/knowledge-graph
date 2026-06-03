"""
Derive UserStory.flows (ordered feature/step names) from story content.
- LLM when OPENAI_API_KEY + USE_LLM_FLOWS (default on if key set)
- Heuristic fallback if LLM fails or disabled
"""

from __future__ import annotations

import json
import logging

import config
from services import graph_service as gs
from services.llm_client import LLMError, chat_json

logger = logging.getLogger(__name__)

_DEMO_FLOW_SEQUENCE = ["Login", "PlanFetch", "PlanSwitch", "Payment", "Analytics"]

_KEYWORD_TO_FEATURE = [
    ("analytics", "Analytics"),
    ("/analytics", "Analytics"),
    ("usage insights", "Analytics"),
    ("login", "Login"),
    ("authenticate", "Login"),
    ("otp", "Login"),
    ("plan fetch", "PlanFetch"),
    ("fetch", "PlanFetch"),
    ("/plans", "PlanFetch"),
    ("get /plans", "PlanFetch"),
    ("recommended plan", "PlanFetch"),
    ("recommended offers", "PlanFetch"),
    ("current plan", "PlanFetch"),
    ("view plan", "PlanFetch"),
    ("switch", "PlanSwitch"),
    ("change plan", "PlanSwitch"),
    ("payment", "Payment"),
    ("pay", "Payment"),
    ("activate", "Payment"),
    ("promo", "Payment"),
]

_EXTRACT_SYSTEM = (
    "You are a QA architect. Extract the ordered user journey from a user story. "
    "Each step must be exactly one feature name from the provided catalog. "
    "Include every catalog feature whose APIs or behaviour are explicitly required in the story text "
    "(e.g. if the story says POST /payments/pay, include Payment when it is in the catalog). "
    "Only omit a feature when the story clearly defers it (e.g. 'payment deferred to a later release') "
    "and does not call that feature's APIs. "
    "Return JSON only."
)


_catalog_cache: list[str] | None = None


def clear_feature_catalog_cache() -> None:
    global _catalog_cache
    _catalog_cache = None


def _feature_catalog() -> list[str]:
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    try:
        features = gs.get_all_features()
    except Exception as e:
        logger.warning("Could not load features from Neo4j: %s", e)
        features = []
    if features:
        names: list[str] = []
        for f in features:
            n = (f.get("name") or f.get("base_id") or "").strip()
            if n and n not in names:
                names.append(n)
        _catalog_cache = names
        return names
    _catalog_cache = list(_DEMO_FLOW_SEQUENCE)
    return _catalog_cache


def _normalize_to_catalog(flows: list, catalog: list[str]) -> list[str]:
    catalog_set = {c: c for c in catalog}
    catalog_lower = {c.lower(): c for c in catalog}
    out: list[str] = []
    seen: set[str] = set()
    for raw in flows:
        name = str(raw).strip()
        if not name:
            continue
        resolved = catalog_set.get(name) or catalog_lower.get(name.lower())
        if not resolved:
            for c in catalog:
                if c.lower() in name.lower() or name.lower() in c.lower():
                    resolved = c
                    break
        if resolved and resolved not in seen:
            out.append(resolved)
            seen.add(resolved)
    return out


def _payment_out_of_scope(content: str) -> bool:
    """True when story text defers billing without calling payment APIs."""
    lower = content.lower()
    if "/payments/" in lower or "post /payments" in lower.replace(" ", ""):
        return False
    markers = ("out of scope", "deferred", "separate release", "not in scope", "future release")
    return any(m in lower for m in markers)


def _flows_from_api_paths_in_content(story: dict) -> list[str]:
    """Map features to story text by matching apis_used paths (deterministic)."""
    content = (story.get("content") or "").lower()
    if not content:
        return []
    try:
        features = gs.get_all_features()
    except Exception as e:
        logger.warning("Could not load features for API path flow match: %s", e)
        return []

    hits: list[tuple[int, str]] = []
    for feat in features:
        name = (feat.get("name") or feat.get("base_id") or "").strip()
        if not name:
            continue
        best_pos = None
        for api in feat.get("apis_used") or []:
            path = str(api).lower().strip()
            if not path:
                continue
            pos = content.find(path)
            if pos >= 0 and (best_pos is None or pos < best_pos):
                best_pos = pos
        if best_pos is not None:
            hits.append((best_pos, name))

    hits.sort(key=lambda x: x[0])
    out: list[str] = []
    seen: set[str] = set()
    for _, name in hits:
        if name not in seen:
            out.append(name)
            seen.add(name)
    return out


def _insert_by_catalog_order(flows: list[str], name: str, catalog: list[str]) -> list[str]:
    """Insert name into flows respecting catalog journey order."""
    if name in flows:
        return flows
    if name not in catalog:
        return flows + [name]
    idx = catalog.index(name)
    insert_at = len(flows)
    for i, existing in enumerate(flows):
        if existing in catalog and catalog.index(existing) > idx:
            insert_at = i
            break
    out = list(flows)
    out.insert(insert_at, name)
    return out


def _merge_flow_lists(primary: list[str], supplemental: list[str], catalog: list[str]) -> list[str]:
    out = list(primary)
    for name in supplemental:
        out = _insert_by_catalog_order(out, name, catalog)
    return _normalize_to_catalog(out, catalog)


def derive_flows_heuristic(story: dict) -> list[str]:
    content = f"{story.get('title', '')} {story.get('content', '')}".lower()
    catalog = _feature_catalog()
    known = set(catalog)

    ordered: list[str] = []
    seen: set[str] = set()

    for keyword, feat in _KEYWORD_TO_FEATURE:
        if keyword not in content or feat not in known or feat in seen:
            continue
        if feat == "Payment" and _payment_out_of_scope(content):
            continue
        ordered.append(feat)
        seen.add(feat)

    catalog = _feature_catalog()
    from_apis = _flows_from_api_paths_in_content(story)
    return _merge_flow_lists(ordered, from_apis, catalog)


def derive_flows_llm(story: dict, *, current_flows: list[str] | None = None) -> dict:
    """
    Call LLM. Returns {"flows": [...], "confidence": float, "evidence": str}.
    """
    catalog = _feature_catalog()
    title = story.get("title", "")
    content = story.get("content", "")
    current = current_flows or []

    if current:
        user = f"""Story title: {title}

Story content:
{content}

Current journey steps on the story (ordered): {json.dumps(current)}

Feature catalog (use ONLY these exact names, in execution order):
{json.dumps(catalog)}

Return JSON:
{{
  "flows": ["FeatureName", ...],
  "confidence": 0.0-1.0,
  "evidence": "brief reason for changes vs current list"
}}
"""
    else:
        user = f"""Story title: {title}

Story content:
{content}

Feature catalog (use ONLY these exact names, in execution order):
{json.dumps(catalog)}

Return JSON:
{{
  "flows": ["FeatureName", ...],
  "confidence": 0.0-1.0,
  "evidence": "brief reason for this ordering"
}}
"""

    result = chat_json(_EXTRACT_SYSTEM, user)
    flows = _normalize_to_catalog(result.get("flows") or [], catalog)
    if not flows:
        return {
            "flows": [],
            "confidence": float(result.get("confidence", 0.7)),
            "evidence": str(
                result.get("evidence", "No catalog features match this story (standalone journey)")
            ),
        }
    return {
        "flows": flows,
        "confidence": float(result.get("confidence", 0.8)),
        "evidence": str(result.get("evidence", "")),
    }


def derive_flows(
    story: dict,
    *,
    use_llm: bool | None = None,
    force: bool = False,
    current_flows: list[str] | None = None,
) -> list[str]:
    """
    Return ordered feature names for UserStory.flows.
    Skips derivation if flows[] already set unless force=True.
    """
    existing = story.get("flows")
    if not force and existing and isinstance(existing, list) and len(existing) > 0:
        return [str(x) for x in existing]

    llm_on = config.USE_LLM_FLOWS if use_llm is None else use_llm
    catalog = _feature_catalog()

    if llm_on and config.OPENAI_API_KEY:
        try:
            out = derive_flows_llm(story, current_flows=current_flows)
            flows = _merge_flow_lists(
                out["flows"],
                _flows_from_api_paths_in_content(story),
                catalog,
            )
            story["_flow_derivation"] = {
                "source": "llm",
                "confidence": out.get("confidence"),
                "evidence": out.get("evidence"),
            }
            return flows
        except LLMError as e:
            logger.warning("LLM flow derivation failed, using heuristic: %s", e)
            story["_flow_derivation"] = {"source": "heuristic_fallback", "error": str(e)}

    flows = derive_flows_heuristic(story)
    story["_flow_derivation"] = story.get("_flow_derivation") or {"source": "heuristic"}
    return flows


def build_flow_steps(proposed: list[str], current: list[str]) -> list[dict]:
    """Build per-step actions for proposals (delta view)."""
    proposed_set = set(proposed)
    steps: list[dict] = []

    for i, name in enumerate(proposed):
        if name not in current:
            action = "create"
        elif current.index(name) != i:
            action = "update"
            reason = "reordered"
        else:
            action = "unchanged"
            reason = None
        step = {"feature_name": name, "action": action}
        if action == "update":
            step["delta_reason"] = reason or "changed"
        steps.append(step)

    for name in current:
        if name not in proposed_set:
            steps.append({"feature_name": name, "action": "deprecate"})
    return steps
