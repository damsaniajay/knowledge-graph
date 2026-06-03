"""Canonical content hashing for duplicate upload detection."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def hash_user_story(item: dict) -> str:
    payload = {
        "title": item.get("title"),
        "content": item.get("content", ""),
        "depends_on": sorted(item.get("depends_on") or []),
        "blocked_by": sorted(item.get("blocked_by") or []),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def hash_feature(item: dict) -> str:
    payload = {
        "name": item.get("name"),
        "description": item.get("description", ""),
        "apis_used": sorted(item.get("apis_used") or []),
        "depends_on": sorted(item.get("depends_on") or []),
        "order": item.get("order", 0),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def hash_test_case(item: dict) -> str:
    linked = item.get("linked_to") or item.get("flow_id") or ""
    deps = item.get("depends_on_test_cases") or []
    if isinstance(deps, str):
        deps = [deps]
    payload = {
        "title": item.get("title"),
        "linked_to": linked,
        "depends_on_test_cases": sorted(str(d).strip() for d in deps if str(d).strip()),
        "type": item.get("type", "positive"),
        "test_layer": item.get("test_layer", "api"),
        "steps": item.get("steps") or [],
        "expected_result": item.get("expected_result", ""),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def hash_response_schema(item: dict) -> str:
    payload = {
        "endpoint_id": item.get("endpoint_id"),
        "status_code": item.get("status_code"),
        "outcome_label": item.get("outcome_label", "default"),
        "schema": item.get("schema") or {},
        "description": item.get("description", ""),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def hash_api_endpoint(item: dict) -> str:
    payload = {
        "path": item.get("path"),
        "method": (item.get("method") or "GET").upper(),
        "summary": item.get("summary", ""),
        "request_schema": item.get("request_schema") or {},
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def hash_openapi_spec(spec: dict) -> str:
    return hashlib.sha256(_canonical_json(spec).encode("utf-8")).hexdigest()


def hash_upload_item(entity_type: str, item: dict, *, raw_bytes: bytes | None = None) -> str:
    if entity_type == "api_spec" and raw_bytes is not None:
        return hash_bytes(raw_bytes)
    if entity_type == "user_story":
        return hash_user_story(item)
    if entity_type == "feature":
        return hash_feature(item)
    if entity_type == "test_case":
        return hash_test_case(item)
    if entity_type == "api_endpoint":
        return hash_api_endpoint(item)
    return hash_bytes(_canonical_json(item).encode("utf-8"))
