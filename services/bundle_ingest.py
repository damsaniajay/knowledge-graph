"""Ingest a knowledge-graph bundle: APIs → features → stories → optional test cases."""

from __future__ import annotations

import logging

from services import graph_service as gs
from services import linking_engine as mapper
from services.entity_identity import resolve_upload_items
from services.openapi_ingest import ingest_openapi
from services.story_flows import prepare_story_flows

logger = logging.getLogger(__name__)


def process_bundle(
    bundle: dict,
    *,
    version_policy: str = "replace",
) -> dict:
    """
    Build the graph from one bundle document.

    Order: OpenAPI (endpoints only) → features → user stories → test cases → re-link.
    """
    spec = bundle["openapi"]
    features = bundle["features"]
    stories = bundle["stories"]
    test_cases = bundle.get("test_cases") or []

    ingested = ingest_openapi(spec, save_response_schemas=False, resync=False)
    feature_ids: list[str] = []
    for feat in features:
        item, _meta = resolve_upload_items("feature", [dict(feat)])
        r = gs.save_feature(item[0], version_policy=version_policy)
        feature_ids.append(item[0]["feature_id"])

    story_results: list[dict] = []
    for raw_story in stories:
        item, _meta = resolve_upload_items("user_story", [dict(raw_story)])
        story = item[0]
        story, _flow = prepare_story_flows(story)
        r = gs.save_user_story(story, version_policy=version_policy)
        story_results.append(
            {
                "story_id": story["story_id"],
                "node_id": r["node_id"],
                "flows": r.get("flows", story.get("flows")),
            }
        )

    tc_count = 0
    for raw_tc in test_cases:
        tc = dict(raw_tc)
        if tc.get("flow_id") and not tc.get("linked_to"):
            tc["linked_to"] = tc["flow_id"]
        item, _meta = resolve_upload_items("test_case", [tc])
        gs.save_test_case(item[0], version_policy=version_policy)
        tc_count += 1

    story_ids = [r["story_id"] for r in story_results if r.get("story_id")]
    edges = mapper.resync_graph(
        story_base_ids=story_ids,
        feature_base_ids=feature_ids,
    )

    primary = story_results[0] if story_results else {}
    return {
        "success": True,
        "entity_type": "bundle",
        "count": len(features) + len(stories) + len(test_cases) + ingested.get("endpoint_count", 0),
        "endpoints_ingested": len(ingested.get("endpoints") or []),
        "features_ingested": len(feature_ids),
        "stories_ingested": len(story_results),
        "test_cases_ingested": tc_count,
        "node_id": primary.get("node_id"),
        "base_id": primary.get("story_id"),
        "story_id": primary.get("story_id"),
        "flows": primary.get("flows"),
        "edges_created": edges,
        "message": (
            f"Bundle built: {len(ingested.get('endpoints') or [])} API(s), "
            f"{len(feature_ids)} feature(s), {len(story_results)} story(ies)"
            + (f", {tc_count} test case(s)" if tc_count else "")
        ),
    }
