"""
relationship_mapper.py  —  Engineer 2
Core engine: called after EVERY entity upload.

Rules:
  When ANY entity is saved, scan existing nodes and create all valid edges.
  New entity → find parent above + children below + siblings → link all.
  Update (v2) → same scan, edges point to new versioned node_id.

No LLM. Matching is purely field-based:
  Flow.story_id           → UserStory.base_id
  Flow.features_used[]    → Feature.name
  Flow.depends_on[]       → Flow.base_id
  Feature.apis_used[]     → APIEndpoint.path  (any method)
  TestCase.flow_id        → Flow.base_id
"""

from services import graph_service as gs


def map_on_upload(entity_type: str, entity_id: str) -> dict:
    """
    Entry point — called after every save_*() call.

    entity_type : 'user_story' | 'feature' | 'api_endpoint' | 'flow' | 'test_case'
    entity_id   : base_id of the entity just saved (e.g. "US1", "Login", "f1")

    Returns {"edges_created": [(from, rel, to), ...]}
    """
    dispatch = {
        "user_story":   _map_user_story,
        "feature":      _map_feature,
        "api_endpoint": _map_api_endpoint,
        "flow":         _map_flow,
        "test_case":    _map_test_case,
    }
    fn = dispatch.get(entity_type)
    if not fn:
        raise ValueError(f"Unknown entity type: {entity_type}")

    edges = fn(entity_id)
    return {"edges_created": edges}


# ─────────────────────────────────────────────────────────────────────────────

def _map_user_story(base_id: str) -> list:
    """
    UserStory uploaded:
      → find all Flows whose story_id == this story's base_id
      → create HAS_FLOW edge
    """
    edges = []
    story = gs.get_user_story(base_id)
    if not story:
        return edges

    for flow in gs.get_all_flows():
        if flow.get("story_id") == base_id:
            created = gs.create_edge(story["node_id"], "HAS_FLOW", flow["node_id"])
            if created:
                edges.append((story["node_id"], "HAS_FLOW", flow["node_id"]))
                print(f"    ↔  HAS_FLOW: {story['node_id']} → {flow['node_id']}")

    return edges


def _map_feature(base_id: str) -> list:
    """
    Feature uploaded:
      → find Flows that list this feature name in features_used → USES_FEATURE
      → find APIEndpoints matching paths in apis_used → CALLS_API
    """
    edges = []
    feature = gs.get_feature(base_id)
    if not feature:
        return edges

    feat_name = feature["name"]
    feat_node = feature["node_id"]
    apis_used = feature.get("apis_used") or []

    # Link FROM flows that reference this feature
    for flow in gs.get_all_flows():
        if feat_name in (flow.get("features_used") or []):
            created = gs.create_edge(flow["node_id"], "USES_FEATURE", feat_node)
            if created:
                edges.append((flow["node_id"], "USES_FEATURE", feat_node))
                print(f"    ↔  USES_FEATURE: {flow['node_id']} → {feat_node}")

    # Link DOWN to API endpoints
    for path in apis_used:
        ep = gs.get_endpoint_by_path(path)
        if ep:
            created = gs.create_edge(feat_node, "CALLS_API", ep["node_id"])
            if created:
                edges.append((feat_node, "CALLS_API", ep["node_id"]))
                print(f"    ↔  CALLS_API: {feat_node} → {ep['node_id']}")

    return edges


def _map_api_endpoint(base_id: str) -> list:
    """
    APIEndpoint uploaded:
      → find Features that list this endpoint's path in apis_used → CALLS_API
    """
    edges = []
    ep = gs.get_endpoint_by_path(
        base_id.split(":", 1)[1] if ":" in base_id else base_id,
        base_id.split(":", 1)[0] if ":" in base_id else None,
    )
    if not ep:
        # fallback: look up by base_id directly
        all_eps = gs.get_all_endpoints()
        ep = next((e for e in all_eps if e["base_id"] == base_id), None)
    if not ep:
        return edges

    ep_path = ep["path"]
    ep_node = ep["node_id"]

    for feature in gs.get_all_features():
        if ep_path in (feature.get("apis_used") or []):
            created = gs.create_edge(feature["node_id"], "CALLS_API", ep_node)
            if created:
                edges.append((feature["node_id"], "CALLS_API", ep_node))
                print(f"    ↔  CALLS_API: {feature['node_id']} → {ep_node}")

    return edges


def _map_flow(base_id: str) -> list:
    """
    Flow uploaded:
      → link UP to UserStory via story_id  → HAS_FLOW
      → link ACROSS to Features via features_used  → USES_FEATURE
      → link DEPENDS_ON to sibling flows  → DEPENDS_ON
      → link DOWN to existing TestCases  → HAS_TEST_CASE
    """
    edges = []
    flow = gs.get_flow(base_id)
    if not flow:
        return edges

    flow_node  = flow["node_id"]
    story_id   = flow.get("story_id", "")
    feat_names = flow.get("features_used") or []
    dep_ids    = flow.get("depends_on") or []

    # Link UP to UserStory
    if story_id:
        story = gs.get_user_story(story_id)
        if story:
            created = gs.create_edge(story["node_id"], "HAS_FLOW", flow_node)
            if created:
                edges.append((story["node_id"], "HAS_FLOW", flow_node))
                print(f"    ↔  HAS_FLOW: {story['node_id']} → {flow_node}")

    # Link ACROSS to Features
    for feat_name in feat_names:
        feature = gs.get_feature_by_name(feat_name)
        if feature:
            created = gs.create_edge(flow_node, "USES_FEATURE", feature["node_id"])
            if created:
                edges.append((flow_node, "USES_FEATURE", feature["node_id"]))
                print(f"    ↔  USES_FEATURE: {flow_node} → {feature['node_id']}")

    # Link DEPENDS_ON sibling flows
    for dep_id in dep_ids:
        dep_flow = gs.get_flow(dep_id)
        if dep_flow:
            created = gs.create_edge(flow_node, "DEPENDS_ON", dep_flow["node_id"])
            if created:
                edges.append((flow_node, "DEPENDS_ON", dep_flow["node_id"]))
                print(f"    ↔  DEPENDS_ON: {flow_node} → {dep_flow['node_id']}")

    # Link DOWN to existing TestCases that reference this flow
    for tc in gs.get_all_test_cases():
        if tc.get("flow_id") == base_id:
            created = gs.create_edge(flow_node, "HAS_TEST_CASE", tc["node_id"])
            if created:
                edges.append((flow_node, "HAS_TEST_CASE", tc["node_id"]))
                print(f"    ↔  HAS_TEST_CASE: {flow_node} → {tc['node_id']}")

    return edges


def _map_test_case(base_id: str) -> list:
    """
    TestCase uploaded:
      → find parent Flow via flow_id  → HAS_TEST_CASE
    """
    edges = []
    tc = gs.get_test_case(base_id)
    if not tc:
        return edges

    tc_node = tc["node_id"]
    flow_id = tc.get("flow_id", "")

    if flow_id:
        flow = gs.get_flow(flow_id)
        if flow:
            created = gs.create_edge(flow["node_id"], "HAS_TEST_CASE", tc_node)
            if created:
                edges.append((flow["node_id"], "HAS_TEST_CASE", tc_node))
                print(f"    ↔  HAS_TEST_CASE: {flow['node_id']} → {tc_node}")

    return edges
