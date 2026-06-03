"""PostgreSQL tracking / history API (optional)."""

from fastapi import APIRouter, HTTPException, Query

from services import postgres_store as pg
from services import tracking

router = APIRouter(prefix="/api/tracking", tags=["tracking"])


@router.get("/health")
def tracking_health():
    return pg.check_connection()


@router.post("/setup")
def setup_tracking():
    result = pg.init_schema()
    if not result.get("ok"):
        raise HTTPException(400, result.get("message", "setup failed"))
    return result


@router.get("/history/{entity_type}/{base_id}")
def entity_history(entity_type: str, base_id: str, limit: int = Query(50, le=200)):
    rows = tracking.list_history(entity_type, base_id, limit=limit)
    return {"entity_type": entity_type, "base_id": base_id, "rows": rows, "source": "postgres"}
