"""
Graph linking engine — runs after every entity upload.

One full re-sync per upload: refresh story flows (LLM) and rebuild all valid edges
regardless of upload order (feature before/after story, API, test case, etc.).
"""

from services import graph_service as gs
from services.flow_derivation import derive_flows
from services.graph_model import REL_BLOCKS, REL_DEPENDS_ON, REL_HAS_FEATURE
from services.graph_model import REL_HAS_RESPONSE_SCHEMA, REL_HAS_TEST_CASE, REL_NEXT_STEP, REL_USES_API
from services.graph_model import REL_VALIDATES_AGAINST


def map_on_upload(entity_type: str, entity_id: str) -> dict:
    """Called once after each upload — always runs a full graph re-sync."""
    if entity_type not in (
        "user_story",
        "feature",
        "api_endpoint",
        "api_response_schema",
        "test_case",
    ):
        raise ValueError(f"Unknown entity type: {entity_type}")
    return {"edges_created": resync_graph()}


def resync_graph() -> list:
    """Public re-sync (CLI refresh, POST /api/graph/relink)."""
    return _resync_graph()


def _resync_graph() -> list:
    edges: list = []
    gs.prune_edges_on_archived_nodes()

    for story in gs.get_all_user_stories():
        edges.extend(_sync_story_flows_and_features(story["base_id"]))
        current = gs.get_user_story(story["base_id"])
        if current:
            edges.extend(_link_story_relationships(current))

    for feature in gs.get_all_features():
        edges.extend(_link_feature_relationships(feature))

    for ep in gs.get_all_endpoints():
        edges.extend(_link_endpoint_relationships(ep))

    for schema in _all_response_schemas():
        edges.extend(_link_response_schema_relationships(schema))

    for tc in gs.get_all_test_cases():
        edges.extend(_link_test_case_relationships(tc))

    return edges


def _link(edge_list: list, triple: tuple) -> None:
    if triple not in edge_list:
        edge_list.append(triple)


def _feature_in_flows(feature: dict, flows: list) -> bool:
    name, fid = feature.get("name"), feature.get("base_id")
    return name in flows or fid in flows


def _feature_display_name(feature: dict) -> str:
    return feature.get("name") or feature.get("base_id") or ""


def _insert_feature_by_depends(feature: dict, flows: list) -> list:
    name = _feature_display_name(feature)
    if not name or name in flows:
        return flows
    for dep in feature.get("depends_on") or []:
        if dep in flows:
            out = list(flows)
            out.insert(flows.index(dep) + 1, name)
            return out
        other = gs.get_feature(dep) or gs.get_feature_by_name(dep)
        if other:
            dep_name = _feature_display_name(other)
            if dep_name in flows:
                out = list(flows)
                out.insert(flows.index(dep_name) + 1, name)
                return out
    return flows


def _sync_story_flows_and_features(story_base_id: str) -> list:
    """LLM/heuristic flows[] + HAS_FEATURE + NEXT_STEP for all stories."""
    edges: list = []
    story = gs.get_user_story(story_base_id)
    if not story:
        return edges

    story_node = story["node_id"]
    old_flows = list(story.get("flows") or [])
    payload = {
        "story_id": story_base_id,
        "title": story.get("title", ""),
        "content": story.get("content", ""),
        "depends_on": story.get("depends_on") or [],
        "blocked_by": story.get("blocked_by") or [],
    }

    try:
        flows = derive_flows(
            payload,
            current_flows=old_flows if old_flows else None,
            force=True,
        )
    except Exception:
        flows = list(old_flows)

    if flows != old_flows:
        payload["flows"] = flows
        gs.save_user_story(payload)
        story = gs.get_user_story(story_base_id) or story
        story_node = story["node_id"]
        flows = list(story.get("flows") or flows)

    for feat in gs.get_all_features():
        if _feature_in_flows(feat, flows):
            if gs.create_edge(story_node, REL_HAS_FEATURE, feat["node_id"]):
                _link(edges, (story_node, REL_HAS_FEATURE, feat["node_id"]))

    _create_next_step_chain(flows, edges)
    return edges


def _link_story_relationships(story: dict) -> list:
    edges: list = []
    story_node = story["node_id"]
    base_id = story["base_id"]

    for dep in story.get("depends_on") or []:
        other = gs.get_user_story(dep)
        if other and gs.create_edge(story_node, REL_DEPENDS_ON, other["node_id"]):
            _link(edges, (story_node, REL_DEPENDS_ON, other["node_id"]))

    for blk in story.get("blocked_by") or []:
        other = gs.get_user_story(blk)
        if other and gs.create_edge(other["node_id"], REL_BLOCKS, story_node):
            _link(edges, (other["node_id"], REL_BLOCKS, story_node))

    content = (story.get("content") or "").lower()
    for ep in gs.get_all_endpoints():
        if ep["path"] in content:
            if gs.create_edge(story_node, REL_USES_API, ep["node_id"]):
                _link(edges, (story_node, REL_USES_API, ep["node_id"]))

    for tc in gs.get_all_test_cases():
        if tc.get("linked_to") in (base_id, story.get("title")):
            if gs.create_edge(story_node, REL_HAS_TEST_CASE, tc["node_id"]):
                _link(edges, (story_node, REL_HAS_TEST_CASE, tc["node_id"]))

    return edges


def _create_next_step_chain(flows: list, edges: list) -> None:
    resolved = []
    for step in flows:
        feat = gs.get_feature(step) or gs.get_feature_by_name(step)
        if feat:
            resolved.append(feat)
    for i in range(len(resolved) - 1):
        a, b = resolved[i], resolved[i + 1]
        if gs.create_edge(a["node_id"], REL_NEXT_STEP, b["node_id"]):
            _link(edges, (a["node_id"], REL_NEXT_STEP, b["node_id"]))


def _link_feature_relationships(feature: dict) -> list:
    edges: list = []
    feat_node = feature["node_id"]
    name = feature.get("name")
    base_id = feature.get("base_id")

    for path in feature.get("apis_used") or []:
        ep = gs.get_endpoint_by_path(path)
        if ep:
            params = _infer_api_params(name, path)
            if gs.create_edge(feat_node, REL_USES_API, ep["node_id"], params=params):
                _link(edges, (feat_node, REL_USES_API, ep["node_id"]))

    for dep in feature.get("depends_on") or []:
        other = gs.get_feature(dep) or gs.get_feature_by_name(dep)
        if other and gs.create_edge(feat_node, REL_DEPENDS_ON, other["node_id"]):
            _link(edges, (feat_node, REL_DEPENDS_ON, other["node_id"]))

    for tc in gs.get_all_test_cases():
        if tc.get("linked_to") in (base_id, name):
            if gs.create_edge(feat_node, REL_HAS_TEST_CASE, tc["node_id"]):
                _link(edges, (feat_node, REL_HAS_TEST_CASE, tc["node_id"]))

    return edges


def _infer_api_params(feature_name: str, path: str) -> str:
    if path == "/plans" and feature_name and "fetch" in feature_name.lower():
        if "recommend" in feature_name.lower():
            return "type=recommended"
        return "type=current"
    return ""


def _link_endpoint_relationships(ep: dict) -> list:
    edges: list = []
    ep_node, path = ep["node_id"], ep["path"]

    for feature in gs.get_all_features():
        if path in (feature.get("apis_used") or []):
            params = _infer_api_params(feature.get("name"), path)
            if gs.create_edge(feature["node_id"], REL_USES_API, ep_node, params=params):
                _link(edges, (feature["node_id"], REL_USES_API, ep_node))

    for story in gs.get_all_user_stories():
        if path in (story.get("content") or ""):
            if gs.create_edge(story["node_id"], REL_USES_API, ep_node):
                _link(edges, (story["node_id"], REL_USES_API, ep_node))

    return edges


def _all_response_schemas() -> list[dict]:
    with gs._get_driver().session() as session:
        return [
            dict(r)
            for r in session.run(
                "MATCH (s:APIResponseSchema {is_current:true}) "
                "RETURN s.node_id AS node_id, s.base_id AS base_id, s.endpoint_id AS endpoint_id"
            )
        ]


def _link_response_schema_relationships(schema: dict) -> list:
    edges: list = []
    eid = schema.get("endpoint_id", "")
    if ":" not in eid:
        return edges
    method, path = eid.split(":", 1)
    ep = gs.get_endpoint_by_path(path, method)
    if ep and gs.create_edge(ep["node_id"], REL_HAS_RESPONSE_SCHEMA, schema["node_id"]):
        _link(edges, (ep["node_id"], REL_HAS_RESPONSE_SCHEMA, schema["node_id"]))
    return edges


def _link_test_case_relationships(tc: dict) -> list:
    edges: list = []
    tc_node = tc["node_id"]
    linked = tc.get("linked_to", "")
    if not linked:
        return edges

    resolved = gs.resolve_entity(linked)
    if resolved:
        _, node = resolved
        if gs.create_edge(node["node_id"], REL_HAS_TEST_CASE, tc_node):
            _link(edges, (node["node_id"], REL_HAS_TEST_CASE, tc_node))

    if tc.get("type") == "negative":
        _link_negative_tc_to_schema(tc, tc_node, edges)

    return edges


def _link_negative_tc_to_schema(tc: dict, tc_node: str, edges: list) -> None:
    linked = tc.get("linked_to", "")
    ep = None
    if ":" in linked:
        ep = gs.get_endpoint_by_path(linked.split(":", 1)[1], linked.split(":", 1)[0])
    else:
        feat = gs.get_feature(linked) or gs.get_feature_by_name(linked)
        if feat and feat.get("apis_used"):
            ep = gs.get_endpoint_by_path(feat["apis_used"][0])
    if not ep:
        return
    for schema in gs.get_response_schemas_for_endpoint(ep["base_id"]):
        if schema.get("status_code", 200) >= 400:
            if gs.create_edge(tc_node, REL_VALIDATES_AGAINST, schema["node_id"]):
                _link(edges, (tc_node, REL_VALIDATES_AGAINST, schema["node_id"]))
