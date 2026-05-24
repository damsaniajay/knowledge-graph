"""
impact_analyser.py
Automatically runs after every v2+ entity upload.

For each entity type, analyses what changed between old and new version,
then surfaces all downstream nodes that are impacted (need review / re-upload).

Entry point: analyse(entity_type, base_id)
  Returns: impact report dict, or None if this was a brand-new (v1) upload.

Impact propagation:
  Flow changed     → TCs of this flow impacted
                   → Flows that DEPEND_ON this flow are flagged
                   → Those flows' TCs are indirectly impacted
  Feature changed  → Flows using this feature impacted → their TCs impacted
  API changed      → Features calling this API impacted
                   → Those features' flows impacted → their TCs impacted
  Story changed    → All flows under story flagged → all their TCs impacted
"""

import json as _json
from services import graph_service as gs


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def analyse(entity_type: str, base_id: str) -> dict | None:
    """
    Called after every upload. Returns None for v1 (nothing was changed).
    """
    dispatch = {
        "user_story":   _analyse_user_story,
        "feature":      _analyse_feature,
        "api_endpoint": _analyse_api_endpoint,
        "flow":         _analyse_flow,
    }
    fn = dispatch.get(entity_type)
    if not fn:
        return None
    return fn(base_id)


# ─────────────────────────────────────────────────────────────────────────────
# Flow impact
# ─────────────────────────────────────────────────────────────────────────────

def _analyse_flow(base_id: str) -> dict | None:
    history = gs.get_flow_history(base_id)
    if len(history) < 2:
        return None

    prev = history[-2]
    curr = history[-1]

    prev_props = gs.get_node_props(prev["node_id"])
    curr_props = gs.get_node_props(curr["node_id"])

    # Feature diff
    prev_features = set(prev_props.get("features_used") or [])
    curr_features  = set(curr_props.get("features_used") or [])
    features_added   = sorted(curr_features - prev_features)
    features_removed = sorted(prev_features - curr_features)

    # Dependency diff
    prev_deps = set(prev_props.get("depends_on") or [])
    curr_deps  = set(curr_props.get("depends_on") or [])
    deps_added   = sorted(curr_deps - prev_deps)
    deps_removed = sorted(prev_deps - curr_deps)

    # Step diff (word-level)
    def _steps_text(props: dict) -> str:
        raw = props.get("steps") or []
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except Exception:
                return raw.lower()
        return " ".join(raw).lower()

    prev_steps = _steps_text(prev_props)
    curr_steps  = _steps_text(curr_props)
    steps_changed = prev_steps != curr_steps

    # Direct TCs — gather from BOTH old and new version nodes (union)
    tcs_old  = gs.get_connected_test_cases(prev["node_id"])
    tcs_curr = gs.get_connected_test_cases(curr["node_id"])
    all_tcs  = {tc["node_id"]: tc for tc in tcs_old + tcs_curr}
    for tc in all_tcs.values():
        tc["reason"] = f"Flow {base_id} content changed (v{prev['version']}→v{curr['version']})"

    # Flows that DEPEND_ON this flow
    dependent_flows = gs.get_flows_depending_on(base_id)

    # TCs of dependent flows (indirect impact)
    indirect_tcs: dict[str, dict] = {}
    for dep_flow in dependent_flows:
        for tc in gs.get_connected_test_cases(dep_flow["node_id"]):
            if tc["node_id"] not in all_tcs and tc["node_id"] not in indirect_tcs:
                tc["via_flow"] = dep_flow["base_id"]
                tc["reason"] = f"Flow {dep_flow['base_id']} depends on {base_id} (changed)"
                indirect_tcs[tc["node_id"]] = tc

    # Missing nodes — features referenced in v2 but not yet in graph
    missing_features = [f for f in curr_features if not gs.get_feature_by_name(f)]

    return {
        "entity_type":    "flow",
        "base_id":        base_id,
        "old_version":    prev["version"],
        "new_version":    curr["version"],
        "steps_changed":  steps_changed,
        "changes": {
            "features_added":    features_added,
            "features_removed":  features_removed,
            "depends_on_added":  deps_added,
            "depends_on_removed": deps_removed,
        },
        "impacted_test_cases":          list(all_tcs.values()),
        "impacted_flows":               dependent_flows,
        "indirect_impacted_test_cases": list(indirect_tcs.values()),
        "missing_nodes": {"features": missing_features},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Feature impact
# ─────────────────────────────────────────────────────────────────────────────

def _analyse_feature(base_id: str) -> dict | None:
    history = gs.get_feature_history(base_id)
    if len(history) < 2:
        return None

    prev = history[-2]
    curr = history[-1]

    prev_props = gs.get_node_props(prev["node_id"])
    curr_props  = gs.get_node_props(curr["node_id"])

    prev_apis = set(prev_props.get("apis_used") or [])
    curr_apis  = set(curr_props.get("apis_used") or [])
    apis_added   = sorted(curr_apis - prev_apis)
    apis_removed = sorted(prev_apis - curr_apis)

    # Flows using this feature
    feature  = gs.get_feature(base_id)
    feat_name = feature["name"] if feature else base_id
    impacted_flows = gs.get_flows_using_feature(feat_name)

    # TCs of those flows
    impacted_tcs: dict[str, dict] = {}
    for flow in impacted_flows:
        for tc in gs.get_connected_test_cases(flow["node_id"]):
            if tc["node_id"] not in impacted_tcs:
                tc["via_flow"]  = flow["base_id"]
                tc["reason"] = f"Feature {base_id} changed — used by flow {flow['base_id']}"
                impacted_tcs[tc["node_id"]] = tc

    # Missing API nodes
    missing_apis = [a for a in curr_apis if not gs.get_endpoint_by_path(a)]

    return {
        "entity_type":  "feature",
        "base_id":      base_id,
        "old_version":  prev["version"],
        "new_version":  curr["version"],
        "changes": {
            "apis_added":   apis_added,
            "apis_removed": apis_removed,
        },
        "impacted_flows":      impacted_flows,
        "impacted_test_cases": list(impacted_tcs.values()),
        "indirect_impacted_test_cases": [],
        "missing_nodes": {"api_endpoints": missing_apis},
    }


# ─────────────────────────────────────────────────────────────────────────────
# API Endpoint impact
# ─────────────────────────────────────────────────────────────────────────────

def _analyse_api_endpoint(base_id: str) -> dict | None:
    history = gs.get_endpoint_history(base_id)
    if len(history) < 2:
        return None

    prev = history[-2]
    curr = history[-1]

    prev_props = gs.get_node_props(prev["node_id"])
    curr_props  = gs.get_node_props(curr["node_id"])

    def _fields(props: dict) -> set[str]:
        raw = props.get("request_schema") or "{}"
        try:
            schema = _json.loads(raw) if isinstance(raw, str) else raw
            return set((schema.get("properties") or {}).keys())
        except Exception:
            return set()

    prev_fields = _fields(prev_props)
    curr_fields  = _fields(curr_props)
    fields_added   = sorted(curr_fields - prev_fields)
    fields_removed = sorted(prev_fields - curr_fields)

    # Features calling this API
    impacted_features = gs.get_features_calling_api(base_id)

    # Flows using those features → TCs
    impacted_flows: dict[str, dict] = {}
    impacted_tcs:   dict[str, dict] = {}

    for feat in impacted_features:
        for flow in gs.get_flows_using_feature(feat["name"]):
            if flow["node_id"] not in impacted_flows:
                flow["via_feature"] = feat["name"]
                impacted_flows[flow["node_id"]] = flow
            for tc in gs.get_connected_test_cases(flow["node_id"]):
                if tc["node_id"] not in impacted_tcs:
                    tc["via_flow"]    = flow["base_id"]
                    tc["via_feature"] = feat["name"]
                    tc["reason"] = (
                        f"API {base_id} schema changed → "
                        f"Feature {feat['name']} → Flow {flow['base_id']}"
                    )
                    impacted_tcs[tc["node_id"]] = tc

    return {
        "entity_type":  "api_endpoint",
        "base_id":      base_id,
        "old_version":  prev["version"],
        "new_version":  curr["version"],
        "changes": {
            "fields_added":   fields_added,
            "fields_removed": fields_removed,
        },
        "impacted_features":   impacted_features,
        "impacted_flows":      list(impacted_flows.values()),
        "impacted_test_cases": list(impacted_tcs.values()),
        "indirect_impacted_test_cases": [],
        "missing_nodes": {},
    }


# ─────────────────────────────────────────────────────────────────────────────
# UserStory impact
# ─────────────────────────────────────────────────────────────────────────────

def _analyse_user_story(base_id: str) -> dict | None:
    history = gs.get_user_story_history(base_id)
    if len(history) < 2:
        return None

    prev = history[-2]
    curr = history[-1]

    prev_props = gs.get_node_props(prev["node_id"])
    curr_props  = gs.get_node_props(curr["node_id"])

    prev_words = set((prev_props.get("content") or "").lower().split())
    curr_words  = set((curr_props.get("content") or "").lower().split())
    words_added   = sorted((curr_words - prev_words))[:20]
    words_removed = sorted((prev_words - curr_words))[:20]

    # All flows under current story version
    story = gs.get_user_story(base_id)
    impacted_flows = gs.get_connected_flows(story["node_id"]) if story else []

    # All TCs of those flows
    impacted_tcs: dict[str, dict] = {}
    for flow in impacted_flows:
        for tc in gs.get_connected_test_cases(flow["node_id"]):
            if tc["node_id"] not in impacted_tcs:
                tc["via_flow"] = flow["base_id"]
                tc["reason"] = f"UserStory {base_id} changed — ancestor of flow {flow['base_id']}"
                impacted_tcs[tc["node_id"]] = tc

    return {
        "entity_type":  "user_story",
        "base_id":      base_id,
        "old_version":  prev["version"],
        "new_version":  curr["version"],
        "changes": {
            "words_added":   words_added,
            "words_removed": words_removed,
        },
        "impacted_flows":      impacted_flows,
        "impacted_test_cases": list(impacted_tcs.values()),
        "indirect_impacted_test_cases": [],
        "missing_nodes": {},
    }
