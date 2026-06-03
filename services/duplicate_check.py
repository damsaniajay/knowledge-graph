"""Detect duplicate uploads by content hash."""

from __future__ import annotations

from services import graph_service as gs
from services.content_hash import hash_bytes, hash_upload_item

LABEL_BY_TYPE = {
    "user_story": "UserStory",
    "feature": "Feature",
    "test_case": "TestCase",
    "api_endpoint": "APIEndpoint",
}


def _display_name(entity_type: str, node: dict, item: dict) -> str:
    if entity_type == "user_story":
        return node.get("title") or item.get("title") or node.get("base_id", "")
    if entity_type == "feature":
        return node.get("name") or item.get("name") or node.get("base_id", "")
    if entity_type == "test_case":
        return node.get("title") or item.get("title") or node.get("base_id", "")
    return node.get("base_id", "")


def find_openapi_duplicate(raw_bytes: bytes) -> dict | None:
    """Same OpenAPI file bytes already ingested."""
    bundle_hash = hash_bytes(raw_bytes)
    node = gs.find_by_openapi_bundle_hash(bundle_hash)
    if not node:
        return None
    sample = f"{node.get('method', 'GET')} {node.get('path', '')}".strip()
    return {
        "entity_type": "api_spec",
        "type_label": "API Spec",
        "base_id": node.get("base_id", ""),
        "name": sample or "OpenAPI spec",
        "node_id": node.get("node_id"),
        "version": node.get("version"),
        "content_hash": bundle_hash,
        "message": "This OpenAPI spec was already uploaded (identical file content).",
    }


def find_duplicate(
    entity_type: str,
    item: dict,
    *,
    raw_bytes: bytes | None = None,
) -> dict | None:
    """
  Return duplicate info if the same content already exists (current version in Neo4j).
    """
    if entity_type == "api_spec":
        if raw_bytes:
            return find_openapi_duplicate(raw_bytes)
        return None

    label = LABEL_BY_TYPE.get(entity_type)
    if not label:
        return None

    content_hash = hash_upload_item(entity_type, item, raw_bytes=raw_bytes)
    node = gs.find_by_content_hash(label, content_hash)
    if not node:
        return None

    base_id = node.get("base_id", "")
    name = _display_name(entity_type, node, item)
    type_label = {
        "user_story": "User Story",
        "feature": "Feature",
        "test_case": "Test Case",
        "api_endpoint": "API Endpoint",
    }.get(entity_type, entity_type)

    return {
        "entity_type": entity_type,
        "type_label": type_label,
        "base_id": base_id,
        "name": name,
        "node_id": node.get("node_id"),
        "version": node.get("version"),
        "content_hash": content_hash,
        "message": (
            f"This {type_label} already exists: {name} ({base_id}) — version {node.get('version')}. "
            "Use Refresh in the UI to load it, or upload with ?force=true for a new version."
        ),
    }


def check_parsed_upload(
    parsed: dict,
    *,
    raw_bytes: bytes | None = None,
) -> list[dict]:
    """Check all items in a parsed upload batch for duplicates."""
    entity_type = parsed["entity_type"]
    duplicates: list[dict] = []

    if entity_type == "api_spec":
        if raw_bytes:
            dup = find_openapi_duplicate(raw_bytes)
            if dup:
                duplicates.append(dup)
        return duplicates

    for item in parsed["items"]:
        dup = find_duplicate(entity_type, item, raw_bytes=raw_bytes)
        if dup:
            duplicates.append(dup)
    return duplicates
