"""
Compare UserStory.flows[] and story content between the current version and the previous one.

Used after story re-upload to highlight graph nodes:
  - added   — new step in flows[]
  - removed — dropped from flows[] (shown inactive in graph)
  - modified — still in flows[] but story scope or linked Feature version changed
"""

from __future__ import annotations

from services import graph_service as gs

# Keywords that indicate how a feature is referenced in story text (demo + heuristic).
_FEATURE_SIGNALS: dict[str, list[str]] = {
    "Login": ["otp", "authenticate", "/auth/login", "login", "session"],
    "PlanFetch": ["/plans", "recommended", "current plan", "view plan", "offers"],
    "PlanSwitch": ["switch", "promo", "promo_code", "/plans/switch", "change plan"],
    "Payment": ["payment", "pay", "activate", "/payments"],
    "Analytics": ["analytics", "/analytics", "usage", "insights"],
}


def _resolve_feature_ref(name: str) -> dict | None:
    if not name:
        return None
    feat = gs.get_feature(name) or gs.get_feature_by_name(name)
    if not feat:
        return {"name": name, "base_id": name, "node_id": None}
    return {
        "name": feat.get("name") or name,
        "base_id": feat.get("base_id") or name,
        "node_id": feat.get("node_id"),
    }


def _enrich_flow_names(names: list) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for raw in names:
        name = str(raw).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ref = _resolve_feature_ref(name)
        if ref:
            out.append(ref)
    return out


def get_story_flows_by_node_id(node_id: str) -> list[str]:
    with gs._get_driver().session() as session:
        row = session.run(
            "MATCH (n:UserStory {node_id: $id}) RETURN n.flows AS flows",
            id=node_id,
        ).single()
        if not row or not row["flows"]:
            return []
        return list(row["flows"])


def _get_story_content(node_id: str) -> str:
    with gs._get_driver().session() as session:
        row = session.run(
            "MATCH (n:UserStory {node_id: $id}) RETURN n.content AS content",
            id=node_id,
        ).single()
        return (row["content"] or "") if row else ""


def _content_signals(content: str, feature_name: str) -> frozenset[str]:
    text = (content or "").lower()
    keys = _FEATURE_SIGNALS.get(feature_name, [feature_name.lower()])
    return frozenset(k for k in keys if k.lower() in text)


def _feature_version_changed_since(feature_name: str, prev_story_node_id: str) -> bool:
    """True if the Feature gained a new version after the previous story was archived."""
    ref = _resolve_feature_ref(feature_name)
    base_id = ref.get("base_id") if ref else None
    if not base_id:
        return False

    history = gs.get_feature_history(base_id)
    if len(history) < 2:
        return False

    with gs._get_driver().session() as session:
        row = session.run(
            "MATCH (n:UserStory {node_id: $id}) "
            "RETURN coalesce(n.valid_to, n.invalid_at) AS valid_to",
            id=prev_story_node_id,
        ).single()
    prev_story_end = row["valid_to"] if row else None
    if not prev_story_end:
        return False

    latest = history[-1]
    latest_from = latest.get("valid_from")
    if not latest_from:
        return False
    return str(latest_from) > str(prev_story_end) and latest.get("version", 1) > 1


def _detect_modified_names(
    names_in_both: list[str],
    prev_content: str,
    curr_content: str,
    prev_story_node_id: str | None,
) -> list[str]:
    modified: list[str] = []
    for raw in names_in_both:
        name = str(raw).strip()
        if not name:
            continue
        story_scope_changed = _content_signals(prev_content, name) != _content_signals(
            curr_content, name
        )
        feature_reversioned = (
            prev_story_node_id is not None
            and _feature_version_changed_since(name, prev_story_node_id)
        )
        if story_scope_changed or feature_reversioned:
            modified.append(name)
    return modified


def compare_flows_delta(
    *,
    previous_flows: list,
    current_flows: list,
    previous_content: str = "",
    current_content: str = "",
    prev_story_node_id: str | None = None,
) -> dict:
    """Classify flow steps vs prior content (preview before save or after version upload)."""
    current_set = {str(f).strip() for f in current_flows if str(f).strip()}
    prev_set = {str(f).strip() for f in previous_flows if str(f).strip()}

    added_names = [f for f in current_flows if str(f).strip() and str(f).strip() not in prev_set]
    removed_names = [f for f in previous_flows if str(f).strip() and str(f).strip() not in current_set]
    in_both = [f for f in current_flows if str(f).strip() in prev_set]
    modified_names = _detect_modified_names(
        in_both, previous_content, current_content, prev_story_node_id
    )

    modified_set = {str(m).strip() for m in modified_names}
    unchanged_names = [f for f in in_both if str(f).strip() not in modified_set]

    added = _enrich_flow_names(added_names)
    removed = _enrich_flow_names(removed_names)
    modified = _enrich_flow_names(modified_names)
    unchanged = _enrich_flow_names(unchanged_names)

    return {
        "current_flows": list(current_flows),
        "previous_flows": list(previous_flows),
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged": unchanged,
        "has_changes": bool(added or removed or modified),
    }


def preview_story_upload_delta(story_item: dict, base_id: str) -> dict:
    """Predict add / modify / remove vs the current story (before upload)."""
    existing = gs.get_user_story(base_id)
    if not existing:
        return {"is_new": True, "has_changes": False, "story_id": base_id}

    work = dict(story_item)
    work["story_id"] = base_id
    from services.story_flows import prepare_story_flows

    work, _ = prepare_story_flows(work)

    delta = compare_flows_delta(
        previous_flows=list(existing.get("flows") or []),
        current_flows=list(work.get("flows") or []),
        previous_content=existing.get("content") or "",
        current_content=work.get("content") or "",
        prev_story_node_id=existing.get("node_id"),
    )
    delta["story_id"] = base_id
    delta["is_new"] = False
    return delta


def compute_story_flow_delta(
    story_base_id: str,
    story_node_id: str | None = None,
) -> dict:
    """
    Delta of flows[] for the selected story version vs its immediate predecessor.

    When story_node_id is set (story dropdown), compares that version only — not the
  latest current version.
    """
    if story_node_id:
        story = gs.get_user_story_version(story_node_id)
    else:
        story = gs.get_user_story(story_base_id)
    if not story:
        return {"story_id": story_base_id, "has_changes": False}

    current_flows = list(story.get("flows") or [])
    curr_content = story.get("content") or ""
    history = gs.get_user_story_history(story_base_id)

    previous_version = None
    previous_flows: list[str] = []
    prev_story_node_id: str | None = None
    prev_content = ""

    current_v = int(story.get("version") or 1)
    prev = next((h for h in history if int(h.get("version") or 0) == current_v - 1), None)

    if not prev:
        return {
            "story_id": story_base_id,
            "story_node_id": story.get("node_id"),
            "version": story.get("version"),
            "previous_version": None,
            "current_flows": current_flows,
            "previous_flows": [],
            "added": [],
            "removed": [],
            "modified": [],
            "unchanged": _enrich_flow_names(current_flows),
            "has_changes": False,
        }

    previous_version = prev.get("version")
    prev_story_node_id = prev.get("node_id")
    previous_flows = get_story_flows_by_node_id(prev_story_node_id)
    prev_content = _get_story_content(prev_story_node_id)

    delta = compare_flows_delta(
        previous_flows=previous_flows,
        current_flows=current_flows,
        previous_content=prev_content,
        current_content=curr_content,
        prev_story_node_id=prev_story_node_id,
    )
    return {
        "story_id": story_base_id,
        "story_node_id": story.get("node_id"),
        "version": story.get("version"),
        "previous_version": previous_version,
        **delta,
    }
