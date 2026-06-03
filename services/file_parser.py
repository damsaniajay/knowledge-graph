"""Parse uploads — schema v2 (no Flow entity files; IDs optional)."""

import json
from typing import Any
import yaml

ENTITY_STORY = "user_story"
ENTITY_FEATURE = "feature"
ENTITY_TEST_CASE = "test_case"
ENTITY_API_SPEC = "api_spec"
ENTITY_BUNDLE = "bundle"


def _load_data(filename: str, content: bytes) -> Any:
    text = content.decode("utf-8-sig")
    lower = filename.lower()
    if lower.endswith((".yaml", ".yml")):
        return yaml.safe_load(text)
    if lower.endswith(".json"):
        return json.loads(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return yaml.safe_load(text)


def detect_entity_type(data: Any) -> str:
    if not isinstance(data, dict):
        raise ValueError("File must contain a JSON object or OpenAPI spec")
    from services.bundle_parser import is_bundle

    if is_bundle(data):
        return ENTITY_BUNDLE
    if "paths" in data and isinstance(data.get("paths"), dict):
        return ENTITY_API_SPEC
    if "path" in data and "method" in data:
        return "api_endpoint"
    if "name" in data and "content" not in data and (
        "apis_used" in data or "description" in data or "depends_on" in data
    ):
        return ENTITY_FEATURE
    if "title" in data and (
        "steps" in data
        or "expected_result" in data
        or "linked_to" in data
        or "test_layer" in data
        or "type" in data
    ):
        return ENTITY_TEST_CASE
    if "title" in data and "content" in data:
        return ENTITY_STORY
    if "tc_id" in data:
        return ENTITY_TEST_CASE
    if "feature_id" in data:
        return ENTITY_FEATURE
    if "story_id" in data:
        return ENTITY_STORY
    raise ValueError(
        "Could not detect type. Use: bundle (openapi + features + user_story), "
        "user story (title+content), feature (name), test case (title+linked_to), or OpenAPI (paths)."
    )


def _validate_item(entity_type: str, data: dict) -> None:
    if entity_type == ENTITY_STORY:
        if not data.get("title"):
            raise ValueError("User story requires 'title'")
        if "content" not in data:
            data["content"] = ""
    elif entity_type == ENTITY_FEATURE:
        if not data.get("name") and not data.get("feature_id"):
            raise ValueError("Feature requires 'name'")
    elif entity_type == ENTITY_TEST_CASE:
        if not data.get("title"):
            raise ValueError("Test case requires 'title'")
    elif entity_type == "api_endpoint":
        if not data.get("path"):
            raise ValueError("API endpoint requires 'path'")


def parse_upload(filename: str, content: bytes, entity_type: str | None = None) -> dict:
    data = _load_data(filename, content)
    detected = entity_type or detect_entity_type(data)

    if detected == ENTITY_BUNDLE:
        from services.bundle_parser import parse_bundle

        return parse_bundle(data)

    if detected == ENTITY_API_SPEC:
        from services.openapi_ingest import parse_openapi
        endpoints, schemas = parse_openapi(data)
        return {
            "entity_type": ENTITY_API_SPEC,
            "items": [{"spec": data}],
            "preview": {
                "title": (data.get("info") or {}).get("title", "API Spec"),
                "endpoint_count": len(endpoints),
                "schema_count": len(schemas),
            },
        }

    if detected == "api_endpoint":
        _validate_item(detected, data)
        return {"entity_type": "api_endpoint", "items": [data], "preview": data}

    _validate_item(detected, data)
    if detected == ENTITY_TEST_CASE and data.get("flow_id") and not data.get("linked_to"):
        data["linked_to"] = data["flow_id"]
    preview = {k: v for k, v in data.items() if k not in ("content",)}
    if data.get("content"):
        preview["content_preview"] = (data["content"] or "")[:120]
    return {"entity_type": detected, "items": [data], "preview": preview}
