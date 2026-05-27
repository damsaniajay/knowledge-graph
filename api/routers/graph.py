"""Graph read and admin endpoints."""

from fastapi import APIRouter, HTTPException, Query

from services import graph_service as gs
from services import linking_engine as mapper
from services import schema_service
from services.story_flow_delta import compute_story_flow_delta

router = APIRouter(prefix="/api/graph", tags=["graph"])


@router.get("/health")
def health():
    return gs.check_connection()


@router.get("")
def get_graph(
    story_id: str | None = Query(
        None,
        description="UserStory base_id (for flow delta vs previous version)",
    ),
    story_node_id: str | None = Query(
        None,
        description="Specific UserStory node_id to focus in the UI",
    ),
):
    if story_node_id:
        version = gs.get_user_story_version(story_node_id)
        if not version:
            raise HTTPException(404, f"UserStory node '{story_node_id}' not found")
        story_id = version["base_id"]
    elif story_id and not gs.user_story_base_id_exists(story_id):
        raise HTTPException(404, f"UserStory '{story_id}' not found")

    if story_node_id:
        graph = gs.get_story_subgraph(story_node_id)
    else:
        graph = gs.get_full_graph(story_base_id=story_id)
    graph["focus_story_id"] = story_id
    graph["focus_story_node_id"] = story_node_id
    if story_id:
        graph["story_flow_delta"] = compute_story_flow_delta(story_id)
    return graph


@router.get("/stories")
def list_stories():
    return {"stories": gs.list_user_story_versions()}


@router.get("/nodes")
def list_nodes():
    """All uploaded nodes currently active in Neo4j."""
    return gs.list_current_nodes()


@router.post("/repair-schema")
def repair_schema():
    """Drop legacy property UNIQUE constraints and repair archived / orphaned version nodes."""
    return schema_service.repair_endpoint_id_collisions()


@router.post("/relink")
def relink_graph():
    """One full re-sync of flows + all edges (same as after any upload)."""
    total = mapper.resync_graph()
    return {
        "edges_created": total,
        "edge_count": len(total),
        "graph": gs.get_full_graph(),
    }


@router.delete("")
def clear_graph(confirm: str = Query(..., description='Must be "yes" to wipe the graph')):
    """Delete every node and relationship in the knowledge graph."""
    if confirm.lower() != "yes":
        raise HTTPException(400, 'Pass confirm=yes to delete the entire knowledge graph')
    result = gs.clear_knowledge_graph()
    return {**result, "graph": gs.get_full_graph()}
