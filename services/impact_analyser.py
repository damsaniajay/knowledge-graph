"""
Impact analyser — schema v2 (no Flow nodes).
Surfaces downstream entities when a node version changes.
"""

from __future__ import annotations

import json as _json

from services import graph_service as gs
from services.story_flow_delta import compute_story_flow_delta, preview_story_upload_delta


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


def preview_user_story_impact(story_item: dict, base_id: str) -> dict | None:
    """Predict impacted test cases before a story version is saved."""
    existing = gs.get_user_story(base_id)
    if not existing:
        return None
    delta = preview_story_upload_delta(story_item, base_id)
    if delta.get("is_new"):
        return None
    return _impact_from_flow_delta(delta, story_node_id=existing.get("node_id"))


def _tc_payload(
    tc: dict,
    *,
    reason: str,
    via_feature: str | None = None,
    hops: int | None = None,
    impact_category: str | None = None,
    triggered_by: str | None = None,
    dependency_chain: list[str] | None = None,
) -> dict:
    row = {
        "node_id": tc.get("node_id"),
        "base_id": tc.get("base_id"),
        "title": tc.get("title") or tc.get("base_id"),
        "type": tc.get("type"),
        "reason": reason,
        "impact_category": impact_category or "direct",
    }
    if via_feature:
        row["via_feature"] = via_feature
    if hops is not None:
        row["hops"] = hops
    if triggered_by:
        row["triggered_by"] = triggered_by
    if dependency_chain:
        row["dependency_chain"] = dependency_chain
    return row


def _collect_tcs_for_feature_ref(
    feat_ref: dict,
    *,
    reason: str,
    impact_category: str,
    direct: dict[str, dict],
) -> None:
    name = feat_ref.get("name") or feat_ref.get("base_id") or ""
    node_id = feat_ref.get("node_id")
    if not node_id and name:
        feat = gs.get_feature(name) or gs.get_feature_by_name(name)
        node_id = feat.get("node_id") if feat else None
    if not node_id:
        return
    for tc in gs.get_test_cases_for_entity(node_id):
        nid = tc.get("node_id")
        if not nid or nid in direct:
            continue
        direct[nid] = _tc_payload(
            tc,
            reason=reason,
            via_feature=name,
            impact_category=impact_category,
        )


def _flow_change_summary(delta: dict) -> str:
    parts: list[str] = []
    added = [f.get("name") for f in delta.get("added") or [] if f.get("name")]
    removed = [f.get("name") for f in delta.get("removed") or [] if f.get("name")]
    modified = [f.get("name") for f in delta.get("modified") or [] if f.get("name")]
    if added:
        parts.append(f"added {', '.join(added)}")
    if removed:
        parts.append(f"removed {', '.join(removed)}")
    if modified:
        parts.append(f"modified {', '.join(modified)}")
    return "; ".join(parts) if parts else "story flow or content changed"


def _dependents_of_tc(base_id: str, *, max_hops: int = 10) -> list[dict]:
    result = gs.get_test_case_impact(base_id, max_hops=max_hops)
    return list(result.get("dependents") or [])


def _impact_from_flow_delta(delta: dict, *, story_node_id: str | None) -> dict:
    """Build direct + indirect test-case impact from a story flow delta."""
    direct: dict[str, dict] = {}

    summary = _flow_change_summary(delta)

    for feat in delta.get("removed") or []:
        fname = feat.get("name") or feat.get("base_id") or "feature"
        _collect_tcs_for_feature_ref(
            feat,
            reason=f"Feature «{fname}» was removed from the story flow ({summary})",
            impact_category="feature_removed",
            direct=direct,
        )
    for feat in delta.get("modified") or []:
        fname = feat.get("name") or feat.get("base_id") or "feature"
        _collect_tcs_for_feature_ref(
            feat,
            reason=f"Feature «{fname}» is still in the flow but story scope changed ({summary})",
            impact_category="feature_modified",
            direct=direct,
        )

    current_flows = [str(f).strip() for f in delta.get("current_flows") or [] if str(f).strip()]
    for feat in delta.get("added") or []:
        add_name = feat.get("name") or ""
        if add_name not in current_flows:
            continue
        idx = current_flows.index(add_name)
        for downstream in current_flows[idx + 1 :]:
            ref = gs.get_feature(downstream) or gs.get_feature_by_name(downstream)
            if ref:
                _collect_tcs_for_feature_ref(
                    {"name": downstream, "node_id": ref.get("node_id"), "base_id": ref.get("base_id")},
                    reason=(
                        f"New flow step «{add_name}» was inserted before «{downstream}» — "
                        f"re-validate steps and assertions ({summary})"
                    ),
                    impact_category="downstream_added",
                    direct=direct,
                )

    story_bid = (delta.get("story_id") or "").strip()
    if story_node_id and story_bid:
        for tc in gs.get_test_cases_for_entity(story_node_id):
            linked = (tc.get("linked_to") or "").strip()
            if linked not in (story_bid, "Plan Change"):
                continue
            nid = tc.get("node_id")
            if not nid or nid in direct:
                continue
            direct[nid] = _tc_payload(
                tc,
                reason=f"Test case is linked to the user story (not a feature); story changed ({summary})",
                impact_category="story_linked",
            )

    if not direct and delta.get("has_changes"):
        story = gs.get_user_story(delta.get("story_id") or "")
        sid = story.get("node_id") if story else story_node_id
        if sid:
            for feat in gs.get_features_for_story(sid):
                fname = feat.get("name") or feat.get("base_id") or "feature"
                _collect_tcs_for_feature_ref(
                    feat,
                    reason=f"Story flow changed ({summary}) — review tests for «{fname}»",
                    impact_category="flow_changed",
                    direct=direct,
                )

    direct_by_base = {row["base_id"]: row for row in direct.values() if row.get("base_id")}

    indirect: dict[str, dict] = {}
    for tc in direct.values():
        bid = tc.get("base_id")
        if not bid:
            continue
        root_reason = tc.get("reason") or "upstream test case impacted"
        for dep in _dependents_of_tc(bid):
            dep_id = dep.get("base_id")
            if not dep_id:
                continue
            dep_node = gs.get_test_case(dep_id)
            if not dep_node:
                continue
            nid = dep_node.get("node_id")
            if not nid or nid in direct:
                continue
            chain = list(dep.get("chain") or [bid, dep_id])
            prereq = dep.get("prerequisite") or (chain[-2] if len(chain) >= 2 else bid)
            chain_txt = " → ".join(chain) if len(chain) > 2 else f"{prereq} → {dep_id}"
            indirect[nid] = _tc_payload(
                dep_node,
                reason=(
                    f"Depends on «{prereq}», which is impacted because «{bid}» changed: "
                    f"{root_reason}"
                ),
                hops=dep.get("hops"),
                impact_category="transitive",
                triggered_by=bid,
                dependency_chain=chain,
            )

    direct_list = sorted(direct.values(), key=lambda x: x.get("base_id") or "")
    indirect_list = sorted(indirect.values(), key=lambda x: (x.get("hops") or 0, x.get("base_id") or ""))

    return {
        "impacted_test_cases": direct_list,
        "indirect_impacted_test_cases": indirect_list,
        "total_test_cases": len(direct_list) + len(indirect_list),
        "impact_summary": summary,
        "flow_delta": {
            "added": [f.get("name") for f in delta.get("added") or [] if f.get("name")],
            "removed": [f.get("name") for f in delta.get("removed") or [] if f.get("name")],
            "modified": [f.get("name") for f in delta.get("modified") or [] if f.get("name")],
        },
    }


def compute_story_upload_impact(
    story_base_id: str,
    story_node_id: str | None = None,
) -> dict | None:
    """Impact for a story version vs its predecessor (for API + UI)."""
    history = gs.get_user_story_history(story_base_id)
    if len(history) < 2:
        return None

    delta = compute_story_flow_delta(story_base_id, story_node_id=story_node_id)
    if delta.get("previous_version") is None:
        return None

    story = (
        gs.get_user_story_version(story_node_id)
        if story_node_id
        else gs.get_user_story(story_base_id)
    )
    if not story:
        return None

    prev_flows = list(delta.get("previous_flows") or [])
    curr_flows = list(delta.get("current_flows") or [])
    tc_impact = _impact_from_flow_delta(delta, story_node_id=story.get("node_id"))
    impacted_features = gs.get_features_for_story(story["node_id"])

    return {
        "entity_type": "user_story",
        "base_id": story_base_id,
        "old_version": delta.get("previous_version"),
        "new_version": delta.get("version"),
        "changes": {
            "flows_added": sorted(set(curr_flows) - set(prev_flows)),
            "flows_removed": sorted(set(prev_flows) - set(curr_flows)),
        },
        "impacted_features": impacted_features,
        **tc_impact,
        "coupling_note": "Review flagged test cases after story flow changes.",
    }


def _analyse_user_story(base_id: str) -> dict | None:
    return compute_story_upload_impact(base_id)


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
            impacted_tcs[tc["node_id"]] = _tc_payload(
                tc, reason=f"Feature {base_id} changed", via_feature=feature.get("name")
            )

    apis = gs.get_apis_for_feature(feat_node) if feat_node else []

    direct_list = list(impacted_tcs.values())
    indirect: dict[str, dict] = {}
    for tc in direct_list:
        bid = tc.get("base_id")
        if not bid:
            continue
        for dep in _dependents_of_tc(bid):
            dep_id = dep.get("base_id")
            if not dep_id:
                continue
            dep_node = gs.get_test_case(dep_id)
            if not dep_node:
                continue
            nid = dep_node.get("node_id")
            if nid and nid not in impacted_tcs:
                indirect[nid] = _tc_payload(
                    dep_node,
                    reason=f"Depends on {bid} (transitive)",
                    hops=dep.get("hops"),
                )

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
        "impacted_test_cases": direct_list,
        "indirect_impacted_test_cases": list(indirect.values()),
        "total_test_cases": len(direct_list) + len(indirect),
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
    if ":" in base_id:
        method, path = base_id.split(":", 1)
        endpoint = gs.get_endpoint_by_path(path, method)
        if endpoint:
            for tc in gs.get_test_cases_for_entity(endpoint["node_id"]):
                impacted_tcs[tc["node_id"]] = _tc_payload(
                    tc,
                    reason=f"API {base_id} contract changed",
                    impact_category="api_contract",
                )

    for feat in features:
        for tc in gs.get_test_cases_for_entity(feat["node_id"]):
            impacted_tcs[tc["node_id"]] = _tc_payload(
                tc,
                reason=f"API {base_id} changed → Feature {feat['name']}",
                via_feature=feat.get("name"),
            )

    direct_list = list(impacted_tcs.values())
    indirect: dict[str, dict] = {}
    for tc in direct_list:
        bid = tc.get("base_id")
        if not bid:
            continue
        for dep in _dependents_of_tc(bid):
            dep_id = dep.get("base_id")
            if not dep_id:
                continue
            dep_node = gs.get_test_case(dep_id)
            if not dep_node:
                continue
            nid = dep_node.get("node_id")
            if nid and nid not in impacted_tcs:
                indirect[nid] = _tc_payload(
                    dep_node,
                    reason=f"Depends on {bid} (transitive)",
                    hops=dep.get("hops"),
                )

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
        "impacted_test_cases": direct_list,
        "indirect_impacted_test_cases": list(indirect.values()),
        "total_test_cases": len(direct_list) + len(indirect),
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
