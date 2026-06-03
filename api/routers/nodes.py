"""CRUD for UserStory, Feature, TestCase — schema v2 (no Flow nodes)."""

from fastapi import APIRouter, HTTPException, Query

from api.schemas import (
    APIEndpointCreate,
    DeleteResponse,
    FeatureCreate,
    NodeMutationResponse,
    TestCaseCreate,
    UserStoryCreate,
)
from services import graph_service as gs
from services import linking_engine as mapper
from services.entity_identity import resolve_item
from services.story_flows import prepare_story_flows, proposal_after_save

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


def _with_graph(story_id: str | None, payload: dict) -> dict:
    payload["graph"] = gs.get_full_graph(story_id)
    return payload


@router.post("/stories", response_model=NodeMutationResponse)
def create_or_update_story(body: UserStoryCreate, story_id: str | None = Query(None)):
    data = body.model_dump()
    data, _identity = resolve_item("user_story", data)
    data, flow_meta = prepare_story_flows(data)
    result = gs.save_user_story(data)
    if flow_meta.get("needs_proposal"):
        flow_meta.update(proposal_after_save(data["story_id"]))
    mapped = mapper.map_on_upload("user_story", data["story_id"])
    sid = data["story_id"]
    msg = "created" if result["is_new"] else "updated"
    if flow_meta.get("proposal_id"):
        msg = "saved; flow proposal pending approval"
    payload = {
        "success": True,
        "node_id": result["node_id"],
        "base_id": data["story_id"],
        "version": result["version"],
        "is_new": result["is_new"],
        "flows": result.get("flows", data["flows"]),
        "edges_created": mapped["edges_created"],
        "message": msg,
    }
    if flow_meta.get("proposal_id"):
        payload["proposal_id"] = flow_meta["proposal_id"]
    if flow_meta.get("flow_derivation"):
        payload["flow_derivation"] = flow_meta["flow_derivation"]
    return _with_graph(sid, payload)


@router.post("/features", response_model=NodeMutationResponse)
def create_or_update_feature(body: FeatureCreate, story_id: str | None = Query(None)):
    data = body.model_dump()
    data, _identity = resolve_item("feature", data)
    result = gs.save_feature(data)
    mapped = mapper.map_on_upload("feature", data["feature_id"])
    return _with_graph(
        story_id,
        {
            "success": True,
            "node_id": result["node_id"],
            "base_id": data["feature_id"],
            "version": result["version"],
            "is_new": result["is_new"],
            "edges_created": mapped["edges_created"],
            "message": "created" if result["is_new"] else "updated",
        },
    )


@router.post("/endpoints", response_model=NodeMutationResponse)
def create_or_update_endpoint(body: APIEndpointCreate, story_id: str | None = Query(None)):
    data = body.model_dump()
    result = gs.save_endpoint(data)
    mapped = mapper.map_on_upload("api_endpoint", result["base_id"])
    return _with_graph(
        story_id,
        {
            "success": True,
            "node_id": result["node_id"],
            "base_id": result["base_id"],
            "version": result["version"],
            "is_new": result["is_new"],
            "edges_created": mapped["edges_created"],
            "message": "created" if result["is_new"] else "updated",
        },
    )


@router.post("/testcases", response_model=NodeMutationResponse)
def create_or_update_testcase(body: TestCaseCreate, story_id: str | None = Query(None)):
    data = body.model_dump()
    if data.get("flow_id") and not data.get("linked_to"):
        data["linked_to"] = data.pop("flow_id")
    data, _identity = resolve_item("test_case", data)
    result = gs.save_test_case(data)
    mapped = mapper.map_on_upload("test_case", data["tc_id"])
    return _with_graph(
        story_id,
        {
            "success": True,
            "node_id": result["node_id"],
            "base_id": data["tc_id"],
            "version": result["version"],
            "is_new": result["is_new"],
            "edges_created": mapped["edges_created"],
            "message": "created" if result["is_new"] else "updated",
        },
    )


@router.delete("/{entity_type}/{base_id}", response_model=DeleteResponse)
def delete_node(entity_type: str, base_id: str, story_id: str | None = Query(None)):
    valid = {"user_story", "feature", "api_endpoint", "api_response_schema", "test_case"}
    if entity_type not in valid:
        raise HTTPException(400, f"entity_type must be one of: {', '.join(sorted(valid))}")
    result = gs.delete_node(entity_type, base_id)
    if not result.get("deleted"):
        raise HTTPException(404, f"{entity_type} '{base_id}' not found")
    return _with_graph(story_id, {"deleted": True, "message": f"Removed {base_id}"})


@router.get("/{entity_type}/{base_id}")
def get_node(entity_type: str, base_id: str):
    if entity_type == "api_endpoint":
        node = gs.get_endpoint_by_path(
            base_id.split(":", 1)[1] if ":" in base_id else base_id,
            base_id.split(":", 1)[0] if ":" in base_id else None,
        )
    else:
        fetchers = {
            "user_story": gs.get_user_story,
            "feature": gs.get_feature,
            "test_case": gs.get_test_case,
            "testcase": gs.get_test_case,
        }
        fn = fetchers.get(entity_type)
        if not fn:
            raise HTTPException(400, "Unknown entity_type")
        node = fn(base_id)
    if not node:
        raise HTTPException(404, "Node not found")
    return node
