"""
Graph linking engine — runs after every entity upload.

One full re-sync per upload: refresh story flows (LLM) and rebuild all valid edges
regardless of upload order (feature before/after story, API, test case, etc.).
"""

import re

import config
from services import graph_service as gs
from services.flow_derivation import clear_feature_catalog_cache, derive_flows
from services.graph_model import REL_BLOCKS, REL_DEPENDS_ON, REL_HAS_FEATURE
from services.graph_model import REL_HAS_RESPONSE_SCHEMA, REL_HAS_TEST_CASE, REL_USES_API
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


def resync_graph(
    *,
    story_base_ids: list[str] | None = None,
    feature_base_ids: list[str] | None = None,
    test_case_base_ids: list[str] | None = None,
    full: bool = False,
) -> list:
    """
    Rebuild edges after upload.

    Default (UPLOAD_FAST): only touch affected stories/features when IDs are passed.
    full=True or no scope: legacy full-graph relink (POST /api/graph/relink).
    """
    if full or not config.UPLOAD_FAST:
        return _resync_graph()
    if story_base_ids or feature_base_ids or test_case_base_ids:
        return _resync_scoped(
            story_base_ids=story_base_ids or [],
            feature_base_ids=feature_base_ids or [],
            test_case_base_ids=test_case_base_ids or [],
        )
    return _resync_graph()


def _resync_scoped(
    *,
    story_base_ids: list[str],
    feature_base_ids: list[str],
    test_case_base_ids: list[str],
) -> list:
    """Relink only entities touched by a recent upload (avoids re-processing every story)."""
    clear_feature_catalog_cache()
    edges: list = []
    _delete_all_feature_next_step_edges()
    gs.prune_edges_on_archived_nodes()

    seen_stories: set[str] = set()
    for sid in story_base_ids:
        if sid and sid not in seen_stories:
            seen_stories.add(sid)
            edges.extend(_sync_story_flows_and_features(sid))
            current = gs.get_user_story(sid)
            if current:
                edges.extend(_link_story_relationships(current))

    seen_features: set[str] = set()
    for fid in feature_base_ids:
        if fid and fid not in seen_features:
            seen_features.add(fid)
            feat = gs.get_feature(fid)
            if not feat:
                continue
            edges.extend(_link_feature_relationships(feat))
            for bid in find_story_base_ids_for_feature(feat):
                if bid not in seen_stories:
                    seen_stories.add(bid)
                    edges.extend(_sync_story_flows_and_features(bid))
                    current = gs.get_user_story(bid)
                    if current:
                        edges.extend(_link_story_relationships(current))
            edges.extend(_materialize_archived_stories_for_feature(feat))

    for ep in gs.get_all_endpoints():
        edges.extend(_link_endpoint_relationships(ep))

    if test_case_base_ids:
        for tc_id in test_case_base_ids:
            tc = gs.get_test_case(tc_id)
            if tc:
                edges.extend(_link_test_case_relationships(tc))
    else:
        for tc in gs.get_all_test_cases():
            edges.extend(_link_test_case_relationships(tc))

    return edges


def _resync_graph() -> list:
    clear_feature_catalog_cache()
    edges: list = []
    _delete_all_feature_next_step_edges()
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

    for tc in gs.get_all_test_cases():
        edges.extend(_link_test_case_relationships(tc))

    return edges


def _link(edge_list: list, triple: tuple) -> None:
    if triple not in edge_list:
        edge_list.append(triple)


def _feature_in_flows(feature: dict, flows: list) -> bool:
    name, fid = feature.get("name"), feature.get("base_id")
    return name in flows or fid in flows


def _resolve_flow_features(flows: list) -> list[dict]:
    resolved: list[dict] = []
    seen: set[str] = set()
    for step in flows:
        feat = gs.get_feature(step) or gs.get_feature_by_name(step)
        if not feat:
            continue
        nid = feat["node_id"]
        if nid in seen:
            continue
        seen.add(nid)
        resolved.append(feat)
    return resolved


def _flow_feature_node_ids(flows: list) -> set[str]:
    return {f["node_id"] for f in _resolve_flow_features(flows)}


def _flow_depends_pairs(flows: list) -> set[tuple[str, str]]:
    """
    Structural dependencies from story flow order (dependent → prerequisite).

    flows [Login, PlanFetch, PlanSwitch] → PlanFetch→Login, PlanSwitch→PlanFetch.
    Order lives on UserStory.flows[]; edges encode dependency only, not workflow steps.
    """
    resolved = _resolve_flow_features(flows)
    return {
        (resolved[i]["node_id"], resolved[i - 1]["node_id"])
        for i in range(1, len(resolved))
    }


def _delete_all_feature_next_step_edges() -> None:
    """Remove legacy workflow NEXT_STEP edges between features (schema uses DEPENDS_ON only)."""
    with gs._get_driver().session() as session:
        session.run("MATCH (:Feature)-[r:NEXT_STEP]->(:Feature) DELETE r")


def _rebuild_flow_depends_on_from_story_order(flows: list) -> None:
    """
    DEPENDS_ON from dependent feature → prerequisite feature, derived from flows[] order.

    Not workflow/orchestration (no NEXT_STEP). Each step depends on the previous step in the list.
    """
    _delete_all_feature_next_step_edges()
    allowed = _flow_feature_node_ids(flows)
    expected = _flow_depends_pairs(flows)

    with gs._get_driver().session() as session:
        for row in session.run(
            "MATCH (a:Feature)-[r:DEPENDS_ON]->(b:Feature) "
            "RETURN a.node_id AS a, b.node_id AS b"
        ):
            a, b = row["a"], row["b"]
            if a in allowed and b in allowed and (a, b) not in expected:
                gs.delete_edge(a, REL_DEPENDS_ON, b)

    for dependent, prerequisite in expected:
        gs.create_edge(dependent, REL_DEPENDS_ON, prerequisite)


def _prune_depends_on_for_flows(flows: list) -> None:
    """Remove DEPENDS_ON into/out of features that are not in this story's flows[]."""
    allowed = _flow_feature_node_ids(flows)
    with gs._get_driver().session() as session:
        for row in session.run(
            "MATCH (a:Feature)-[r:DEPENDS_ON]->(b:Feature) "
            "RETURN a.node_id AS a, b.node_id AS b"
        ):
            if row["a"] not in allowed or row["b"] not in allowed:
                gs.delete_edge(row["a"], REL_DEPENDS_ON, row["b"])


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


def find_story_base_ids_for_feature(feature: dict) -> list[str]:
    """Story base_ids that reference this feature (any version's flows[] or live content APIs)."""
    name = (feature.get("name") or feature.get("base_id") or "").strip()
    fid = feature.get("base_id") or ""
    apis = [p.lower() for p in (feature.get("apis_used") or []) if p]
    found: set[str] = set()

    with gs._get_driver().session() as session:
        for row in session.run(
            "MATCH (s:UserStory) "
            "RETURN s.base_id AS base_id, s.flows AS flows, s.content AS content, "
            "coalesce(s.is_current, false) AS is_current"
        ):
            base_id = row["base_id"]
            flows = list(row["flows"] or [])
            if name and (name in flows or fid in flows):
                found.add(base_id)
                continue
            if apis:
                content = (row["content"] or "").lower()
                if any(p in content for p in apis):
                    found.add(base_id)
    return list(found)


def _materialize_archived_stories_for_feature(feature: dict) -> list:
    """Rebuild edges on archived story versions that list this feature in flows[]."""
    name = (feature.get("name") or feature.get("base_id") or "").strip()
    fid = feature.get("base_id") or ""
    edges: list = []
    with gs._get_driver().session() as session:
        for row in session.run(
            """
            MATCH (s:UserStory)
            WHERE coalesce(s.is_current, false) = false
            RETURN s.node_id AS node_id, s.flows AS flows
            """
        ):
            flows = list(row["flows"] or [])
            if name and (name in flows or fid in flows):
                edges.extend(materialize_story_version_links(row["node_id"]))
    return edges


def materialize_story_version_links(story_node_id: str) -> list:
    """
    Ensure a specific story version (live or archived) has edges from its flows[] and content.
    Does not remove edges from other story versions.
    """
    story = gs.get_user_story_version(story_node_id)
    if not story:
        return []

    edges: list = []
    story_node = story_node_id
    flows = list(story.get("flows") or [])
    content = (story.get("content") or "").lower()
    linked_features: set[str] = set()

    for feat in gs.get_all_features():
        if _feature_in_flows(feat, flows):
            linked_features.add(feat["node_id"])
            if gs.create_edge(story_node, REL_HAS_FEATURE, feat["node_id"]):
                _link(edges, (story_node, REL_HAS_FEATURE, feat["node_id"]))

    for feat in gs.get_all_features():
        if feat["node_id"] in linked_features:
            continue
        for path in feat.get("apis_used") or []:
            if path and path.lower() in content:
                linked_features.add(feat["node_id"])
                if gs.create_edge(story_node, REL_HAS_FEATURE, feat["node_id"]):
                    _link(edges, (story_node, REL_HAS_FEATURE, feat["node_id"]))
                break

    for linked in gs.get_features_for_story(story_node):
        if linked["node_id"] not in linked_features:
            gs.delete_edge(story_node, REL_HAS_FEATURE, linked["node_id"])

    for ep in gs.get_all_endpoints():
        if ep.get("path") and str(ep["path"]).lower() in content:
            if gs.create_edge(story_node, REL_USES_API, ep["node_id"]):
                _link(edges, (story_node, REL_USES_API, ep["node_id"]))

    _rebuild_flow_depends_on_from_story_order(flows)
    _prune_depends_on_for_flows(flows)
    return edges


def _sync_story_flows_and_features(story_base_id: str) -> list:
    """LLM/heuristic flows[] + HAS_FEATURE + flow-order DEPENDS_ON for the live story version."""
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

    if old_flows:
        flows = list(old_flows)
    else:
        use_llm = config.USE_LLM_FLOWS and config.OPENAI_API_KEY and not config.UPLOAD_FAST
        try:
            flows = derive_flows(payload, use_llm=use_llm)
        except Exception:
            flows = list(old_flows)

    if flows != old_flows:
        payload["flows"] = flows
        gs.save_user_story(payload)
        story = gs.get_user_story(story_base_id) or story
        story_node = story["node_id"]
        flows = list(story.get("flows") or flows)

    desired_feature_ids: set[str] = set()
    for feat in gs.get_all_features():
        if _feature_in_flows(feat, flows):
            desired_feature_ids.add(feat["node_id"])
            if gs.create_edge(story_node, REL_HAS_FEATURE, feat["node_id"]):
                _link(edges, (story_node, REL_HAS_FEATURE, feat["node_id"]))

    for linked in gs.get_features_for_story(story_node):
        if linked["node_id"] not in desired_feature_ids:
            gs.delete_edge(story_node, REL_HAS_FEATURE, linked["node_id"])

    _rebuild_flow_depends_on_from_story_order(flows)
    _prune_depends_on_for_flows(flows)
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

    # Even if flows[] are still pending/empty (FLOW_REQUIRE_APPROVAL=true),
    # connect story -> features using the endpoints mentioned in the story text.
    # This keeps feature-linked test cases reachable in the UI.
    for feat in gs.get_all_features():
        feat_node = feat["node_id"]
        for path in feat.get("apis_used") or []:
            if path and path.lower() in content:
                if gs.create_edge(story_node, REL_HAS_FEATURE, feat_node):
                    _link(edges, (story_node, REL_HAS_FEATURE, feat_node))
                break

    for tc in gs.get_all_test_cases():
        if tc.get("linked_to") in (base_id, story.get("title")):
            if gs.create_edge(story_node, REL_HAS_TEST_CASE, tc["node_id"]):
                _link(edges, (story_node, REL_HAS_TEST_CASE, tc["node_id"]))

    return edges


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
    """
    Ensure test cases connect in 3 ways:
      1) UserStory -[:HAS_TEST_CASE]-> TestCase
      2) Feature    -[:HAS_TEST_CASE]-> TestCase
      3) APIEndpoint-[:HAS_TEST_CASE]-> TestCase
    And:
      4) TestCase -[:VALIDATES_AGAINST]-> APIResponseSchema (using HTTP status_code from expected_result)
    """
    edges: list = []
    tc_node = tc["node_id"]

    linked = (tc.get("linked_to") or "").strip()
    if not linked:
        return edges

    expected_result = tc.get("expected_result") or ""
    expected_code: int | None = None
    # Prefer "HTTP 401" style.
    m = re.search(r"HTTP\\s*([0-9]{3})", str(expected_result), flags=re.IGNORECASE)
    if m:
        expected_code = int(m.group(1))
    else:
        # Fallback: first 3-digit token (best-effort for "HTTP 200 — ...").
        m2 = re.search(r"\\b([0-9]{3})\\b", str(expected_result))
        if m2:
            expected_code = int(m2.group(1))

    resolved = gs.resolve_entity(linked)
    if not resolved:
        return edges

    label, node = resolved
    endpoints: list[dict] = []

    # Link TestCase based on resolved entity type.
    if label == "Feature":
        # Feature -> HAS_TEST_CASE
        if gs.create_edge(node["node_id"], REL_HAS_TEST_CASE, tc_node):
            _link(edges, (node["node_id"], REL_HAS_TEST_CASE, tc_node))

        # Also link UserStories that include this feature.
        for s in gs.get_stories_linking_feature(node["base_id"]):
            if gs.create_edge(s["node_id"], REL_HAS_TEST_CASE, tc_node):
                _link(edges, (s["node_id"], REL_HAS_TEST_CASE, tc_node))

        # And connect APIEndpoint -> HAS_TEST_CASE (from feature.apis_used)
        for path in node.get("apis_used") or []:
            ep = gs.get_endpoint_by_path(path)
            if not ep:
                continue
            endpoints.append(ep)
            if gs.create_edge(ep["node_id"], REL_HAS_TEST_CASE, tc_node):
                _link(edges, (ep["node_id"], REL_HAS_TEST_CASE, tc_node))

    elif label == "UserStory":
        # UserStory -> HAS_TEST_CASE
        if gs.create_edge(node["node_id"], REL_HAS_TEST_CASE, tc_node):
            _link(edges, (node["node_id"], REL_HAS_TEST_CASE, tc_node))

        # Infer endpoints from story content.
        content = str(node.get("content") or "").lower()
        for ep in gs.get_all_endpoints():
            if ep.get("path") and str(ep["path"]).lower() in content:
                endpoints.append(ep)
                if gs.create_edge(ep["node_id"], REL_HAS_TEST_CASE, tc_node):
                    _link(edges, (ep["node_id"], REL_HAS_TEST_CASE, tc_node))

    elif label == "APIEndpoint":
        # APIEndpoint -> HAS_TEST_CASE
        if gs.create_edge(node["node_id"], REL_HAS_TEST_CASE, tc_node):
            _link(edges, (node["node_id"], REL_HAS_TEST_CASE, tc_node))
        endpoints = [node]

        # If the APIEndpoint is used by a story, link that story directly too.
        for s in gs.get_stories_using_api(node["base_id"]):
            if gs.create_edge(s["node_id"], REL_HAS_TEST_CASE, tc_node):
                _link(edges, (s["node_id"], REL_HAS_TEST_CASE, tc_node))

    # Link to response schema(s) based on expected HTTP code.
    if endpoints:
        for ep in endpoints:
            schemas = gs.get_response_schemas_for_endpoint(ep["base_id"])

            picked = None
            if expected_code is not None:
                for schema in schemas:
                    try:
                        if int(schema.get("status_code")) == expected_code:
                            picked = schema
                            break
                    except Exception:
                        continue

            # If we couldn't match exact status, but it's a negative test, pick best-effort.
            if not picked and tc.get("type") == "negative":
                for schema in schemas:
                    try:
                        if int(schema.get("status_code", 200)) >= 400:
                            picked = schema
                            break
                    except Exception:
                        continue

            if picked:
                if gs.create_edge(tc_node, REL_VALIDATES_AGAINST, picked["node_id"]):
                    _link(edges, (tc_node, REL_VALIDATES_AGAINST, picked["node_id"]))

    return edges
