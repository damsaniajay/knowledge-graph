"""Optional PostgreSQL connection for tracking tables."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import config

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "001_tracking.sql"


def enabled() -> bool:
    return bool(config.DATABASE_URL and config.TRACKING_ENABLED)


@contextmanager
def connection() -> Iterator[Any]:
    if not enabled():
        yield None
        return
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError as e:
        logger.warning("psycopg2 not installed — tracking disabled: %s", e)
        yield None
        return

    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema() -> dict:
    """Create tracking tables. No-op if DATABASE_URL unset."""
    if not config.DATABASE_URL:
        return {"ok": False, "message": "DATABASE_URL not set"}
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    with connection() as conn:
        if conn is None:
            return {"ok": False, "message": "Could not connect (install psycopg2-binary?)"}
        with conn.cursor() as cur:
            cur.execute(sql)
    return {"ok": True, "message": "Tracking schema applied", "path": str(_SCHEMA_PATH)}


def check_connection() -> dict:
    if not config.DATABASE_URL:
        return {"connected": False, "message": "DATABASE_URL not set"}
    try:
        with connection() as conn:
            if conn is None:
                return {"connected": False, "message": "psycopg2 unavailable"}
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                row = cur.fetchone()
            return {"connected": bool(row and row[0] == 1)}
    except Exception as e:
        return {"connected": False, "error": str(e)}
