"""
Impact analyser — schema v2 (no Flow nodes).
Surfaces downstream entities when a node version changes.
"""

import json as _json

from services import graph_service as gs


def analyse(entity_type: str, base_id: str) -> dict | None:
    dispatch = {
        "user_story": _analyse_user_story,
        "feature": _analyse_feature,
        "api_endpoint": _analyse_api_endpoint,
        "test_case": _analyse_test_case,
    }
    fn = dispatch.get(entity_type)
    if not fn:
        return None
    return fn(base_id)


def _analyse_user_story(base_id: str) -> dict | None:
    history = gs.get_user_story_history(base_id)
    if len(history) < 2:
        return None

    prev, curr = history[-2], history[-1]
    prev_p, curr_p = gs.get_node_props(prev["node_id"]), gs.get_node_props(curr["node_id"])

    prev_flows = list(prev_p.get("flows") or [])
    curr_flows = list(curr_p.get("flows") or [])

    story = gs.get_user_story(base_id)
    impacted_features = gs.get_features_for_story(story["node_id"]) if story else []
    impacted_tcs: dict[str, dict] = {}
    for feat in impacted_features:
        for tc in gs.get_test_cases_for_entity(feat["node_id"]):
            impacted_tcs[tc["node_id"]] = {**tc, "reason": f"UserStory {base_id} changed"}

    if story:
        for tc in gs.get_test_cases_for_entity(story["node_id"]):
            impacted_tcs[tc["node_id"]] = {**tc, "reason": f"UserStory {base_id} changed"}

    return {
        "entity_type": "user_story",
        "base_id": base_id,
        "old_version": prev["version"],
        "new_version": curr["version"],
        "changes": {
            "flows_added": sorted(set(curr_flows) - set(prev_flows)),
            "flows_removed": sorted(set(prev_flows) - set(curr_flows)),
        },
        "impacted_features": impacted_features,
        "impacted_test_cases": list(impacted_tcs.values()),
        "coupling_note": "Cascade depth respects USES_API.coupling_type (tight=full regen, loose=re-validate)",
    }


def _analyse_feature(base_id: str) -> dict | None:
    history = gs.get_feature_history(base_id)
    if len(history) < 2:
        return None

    prev, curr = history[-2], history[-1]
    prev_p, curr_p = gs.get_node_props(prev["node_id"]), gs.get_node_props(curr["node_id"])

    prev_apis = set(prev_p.get("apis_used") or [])
    curr_apis = set(curr_p.get("apis_used") or [])

    feature = gs.get_feature(base_id)
    feat_node = feature["node_id"] if feature else None

    impacted_tcs: dict[str, dict] = {}
    if feat_node:
        for tc in gs.get_test_cases_for_entity(feat_node):
            impacted_tcs[tc["node_id"]] = {**tc, "reason": f"Feature {base_id} changed"}

    apis = gs.get_apis_for_feature(feat_node) if feat_node else []

    return {
        "entity_type": "feature",
        "base_id": base_id,
        "old_version": prev["version"],
        "new_version": curr["version"],
        "changes": {
            "apis_added": sorted(curr_apis - prev_apis),
            "apis_removed": sorted(prev_apis - curr_apis),
        },
        "impacted_api_endpoints": apis,
        "impacted_test_cases": list(impacted_tcs.values()),
        "coverage_gap": _feature_coverage_gap(base_id),
    }


def _analyse_api_endpoint(base_id: str) -> dict | None:
    history = gs.get_endpoint_history(base_id)
    if len(history) < 2:
        return None

    prev, curr = history[-2], history[-1]
    prev_p, curr_p = gs.get_node_props(prev["node_id"]), gs.get_node_props(curr["node_id"])

    def _fields(props: dict) -> set[str]:
        raw = props.get("request_schema") or "{}"
        try:
            schema = _json.loads(raw) if isinstance(raw, str) else raw
            return set((schema.get("properties") or {}).keys())
        except Exception:
            return set()

    prev_f, curr_f = _fields(prev_p), _fields(curr_p)
    features = gs.get_features_using_api(base_id)

    impacted_tcs: dict[str, dict] = {}
    for feat in features:
        for tc in gs.get_test_cases_for_entity(feat["node_id"]):
            impacted_tcs[tc["node_id"]] = {
                **tc,
                "reason": f"API {base_id} changed → Feature {feat['name']}",
            }

    return {
        "entity_type": "api_endpoint",
        "base_id": base_id,
        "old_version": prev["version"],
        "new_version": curr["version"],
        "changes": {
            "fields_added": sorted(curr_f - prev_f),
            "fields_removed": sorted(prev_f - curr_f),
        },
        "impacted_features": features,
        "impacted_test_cases": list(impacted_tcs.values()),
        "response_schema_review": "Re-evaluate APIResponseSchema rows for negative test gaps",
    }


def _analyse_test_case(base_id: str) -> dict | None:
    history = gs.get_test_case_history(base_id)
    if len(history) < 2:
        return None
    return {
        "entity_type": "test_case",
        "base_id": base_id,
        "message": "No downstream impact. Coverage check re-run on parent.",
        "coverage_gap": _coverage_for_linked(base_id),
    }


def _feature_coverage_gap(feature_base_id: str) -> list[str]:
    """Every Feature needs ≥1 positive and ≥1 negative TC."""
    gaps = []
    feature = gs.get_feature(feature_base_id)
    if not feature:
        return ["Feature not found"]
    tcs = gs.get_test_cases_for_entity(feature["node_id"])
    types = {tc.get("type") for tc in tcs}
    if "positive" not in types:
        gaps.append("Missing positive TestCase")
    if "negative" not in types:
        gaps.append("Missing negative TestCase")
    return gaps


def _coverage_for_linked(tc_base_id: str) -> list[str]:
    tc = gs.get_test_case(tc_base_id)
    if not tc:
        return []
    linked = tc.get("linked_to", "")
    feat = gs.get_feature(linked) or gs.get_feature_by_name(linked)
    if feat:
        return _feature_coverage_gap(feat["base_id"])
    return []
