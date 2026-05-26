"""Edge create/delete — reflected immediately in Neo4j."""

from fastapi import APIRouter, HTTPException, Query

from api.schemas import DeleteResponse, EdgeCreate
from services import graph_service as gs

router = APIRouter(prefix="/api/edges", tags=["edges"])


@router.post("")
def create_edge(body: EdgeCreate, story_id: str | None = Query(None)):
    try:
        created = gs.create_edge(
            body.from_node_id,
            body.rel_type,
            body.to_node_id,
            params=body.params,
            coupling_type=body.coupling_type,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return {
        "success": True,
        "created": created,
        "graph": gs.get_full_graph(story_id),
    }


@router.delete("")
def delete_edge(
    from_node_id: str = Query(...),
    rel_type: str = Query(...),
    to_node_id: str = Query(...),
    story_id: str | None = Query(None),
):
    try:
        result = gs.delete_edge(from_node_id, rel_type, to_node_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if not result["deleted"]:
        raise HTTPException(404, "Edge not found")

    return {
        "deleted": True,
        "graph": gs.get_full_graph(story_id),
    }
