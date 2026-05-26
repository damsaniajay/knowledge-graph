"""
Write doc-aligned history rows to PostgreSQL (optional).

Neo4j = live graph. Postgres = audit trail, upload log, structured delta records.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from services import postgres_store as pg

logger = logging.getLogger(__name__)

ENTITY_LABELS = {
    "user_story": "UserStory",
    "feature": "Feature",
    "api_endpoint": "APIEndpoint",
    "api_response_schema": "APIResponseSchema",
    "test_case": "TestCase",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_snapshot(entity_type: str, data: dict) -> dict:
    """Small JSON snapshot for history row (not full Neo4j clone)."""
    if entity_type == "user_story":
        return {
            "title": data.get("title"),
            "content_len": len(data.get("content") or ""),
            "flows": data.get("flows") or [],
        }
    if entity_type == "feature":
        return {"name": data.get("name"), "apis_used": data.get("apis_used") or []}
    if entity_type == "test_case":
        return {
            "title": data.get("title"),
            "type": data.get("type"),
            "linked_to": data.get("linked_to"),
        }
    if entity_type == "api_endpoint":
        return {"method": data.get("method"), "path": data.get("path")}
    return {k: v for k, v in data.items() if k not in ("content",)}


def on_version_saved(
    entity_type: str,
    base_id: str,
    data: dict,
    *,
    node_id: str,
    version: int,
    is_new: bool,
    content_hash: str | None = None,
    version_policy: str = "deprecate",
    created_by: str = "system",
    valid_from: str | None = None,
) -> None:
    if not pg.enabled():
        return
    label = ENTITY_LABELS.get(entity_type, entity_type)
    now = valid_from or _now()
    try:
        with pg.connection() as conn:
            if conn is None:
                return
            with conn.cursor() as cur:
                if not is_new and version_policy != "delete":
                    cur.execute(
                        """
                        UPDATE entity_history
                        SET is_current = false, valid_to = %s
                        WHERE entity_type = %s AND base_id = %s AND is_current = true
                        """,
                        (now, label, base_id),
                    )
                cur.execute(
                    """
                    INSERT INTO entity_history (
                        entity_type, base_id, node_id, version, status, is_current,
                        valid_from, valid_to, content_hash, version_policy, payload, created_by
                    ) VALUES (%s,%s,%s,%s,'active',true,%s,NULL,%s,%s,%s,%s)
                    ON CONFLICT (entity_type, node_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        is_current = EXCLUDED.is_current,
                        valid_from = EXCLUDED.valid_from,
                        content_hash = EXCLUDED.content_hash,
                        payload = EXCLUDED.payload
                    """,
                    (
                        label,
                        base_id,
                        node_id,
                        version,
                        now,
                        content_hash,
                        version_policy,
                        json.dumps(_payload_snapshot(entity_type, data)),
                        created_by,
                    ),
                )
    except Exception as e:
        logger.warning("Postgres entity_history write failed: %s", e)


def on_upload(
    entity_type: str,
    *,
    base_id: str | None = None,
    node_id: str | None = None,
    version: int | None = None,
    filename: str | None = None,
    version_policy: str = "deprecate",
    identity_meta: list[dict] | dict | None = None,
    content_hash: str | None = None,
    extra: dict | None = None,
) -> None:
    if not pg.enabled():
        return
    meta = identity_meta
    if isinstance(meta, list) and meta:
        meta = meta[0]
    meta = meta or {}
    try:
        with pg.connection() as conn:
            if conn is None:
                return
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO upload_events (
                        entity_type, base_id, node_id, version, filename, version_policy,
                        is_new_entity, identity_source, delta_summary, content_hash, extra
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        entity_type,
                        base_id,
                        node_id,
                        version,
                        filename,
                        version_policy,
                        meta.get("is_new_entity"),
                        meta.get("identity_source"),
                        meta.get("delta_summary"),
                        content_hash,
                        json.dumps(extra) if extra else None,
                    ),
                )
                if meta.get("delta_summary") and base_id and node_id:
                    cur.execute(
                        """
                        INSERT INTO delta_events (
                            entity_type, base_id, from_node_id, to_node_id,
                            from_version, to_version, delta_source, delta_summary
                        ) VALUES (%s,%s,NULL,%s,%s,%s,%s,%s)
                        """,
                        (
                            entity_type,
                            base_id,
                            node_id,
                            (version or 1) - 1 if version and version > 1 else None,
                            version,
                            meta.get("identity_source", "llm"),
                            meta.get("delta_summary"),
                        ),
                    )
    except Exception as e:
        logger.warning("Postgres upload_events write failed: %s", e)


def list_history(entity_type: str, base_id: str, limit: int = 50) -> list[dict]:
    label = ENTITY_LABELS.get(entity_type, entity_type)
    if not pg.enabled():
        return []
    try:
        with pg.connection() as conn:
            if conn is None:
                return []
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT node_id, version, status, is_current, valid_from, valid_to,
                           content_hash, version_policy, payload, created_by
                    FROM entity_history
                    WHERE entity_type = %s AND base_id = %s
                    ORDER BY version DESC
                    LIMIT %s
                    """,
                    (label, base_id, limit),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.warning("Postgres list_history failed: %s", e)
        return []
