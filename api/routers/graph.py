"""Graph read and admin endpoints."""

from fastapi import APIRouter, HTTPException, Query

from services import graph_service as gs
from services import linking_engine as mapper
from services import schema_service

router = APIRouter(prefix="/api/graph", tags=["graph"])


@router.get("/health")
def health():
    return gs.check_connection()


@router.get("")
def get_graph(
    story_id: str | None = Query(
        None,
        description="Optional story to highlight (all nodes are always returned)",
    ),
):
    if story_id and not gs.get_user_story(story_id):
        raise HTTPException(404, f"UserStory '{story_id}' not found")
    graph = gs.get_full_graph()
    graph["focus_story_id"] = story_id
    return graph


@router.get("/stories")
def list_stories():
    return {"stories": gs.get_all_user_stories()}


@router.get("/nodes")
def list_nodes():
    """All uploaded nodes currently active in Neo4j."""
    return gs.list_current_nodes()


@router.post("/repair-schema")
def repair_schema():
    """Drop legacy endpoint_id UNIQUE constraint and fix archived API nodes."""
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
