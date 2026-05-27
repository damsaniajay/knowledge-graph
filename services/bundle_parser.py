"""Parse bulk knowledge-graph bundle uploads (OpenAPI + features + story in one file)."""

from __future__ import annotations

from typing import Any

ENTITY_BUNDLE = "bundle"


def is_bundle(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("kind") == "knowledge_graph_bundle":
        return True
    has_features = isinstance(data.get("features"), list) and len(data["features"]) > 0
    has_api = isinstance(data.get("openapi"), dict) or isinstance(data.get("openapi_spec"), dict)
    has_story = isinstance(data.get("user_story"), dict) or isinstance(data.get("stories"), list)
    return has_features and has_api and has_story


def parse_bundle(data: dict) -> dict:
    if not is_bundle(data):
        raise ValueError(
            "Bundle must include openapi/openapi_spec, features[], and user_story (or stories[])"
        )

    spec = data.get("openapi") or data.get("openapi_spec")
    if not isinstance(spec, dict) or "paths" not in spec:
        raise ValueError("Bundle openapi section must be an OpenAPI object with paths")

    features = [f for f in (data.get("features") or []) if isinstance(f, dict)]
    if not features:
        raise ValueError("Bundle requires at least one feature in features[]")

    stories: list[dict] = []
    if isinstance(data.get("user_story"), dict):
        stories.append(data["user_story"])
    for s in data.get("stories") or []:
        if isinstance(s, dict):
            stories.append(s)
    if not stories:
        raise ValueError("Bundle requires user_story or stories[]")

    test_cases = [t for t in (data.get("test_cases") or data.get("testcases") or []) if isinstance(t, dict)]

    return {
        "entity_type": ENTITY_BUNDLE,
        "items": [{"bundle": data}],
        "preview": {
            "title": data.get("title") or (spec.get("info") or {}).get("title", "Knowledge graph bundle"),
            "feature_count": len(features),
            "story_count": len(stories),
            "test_case_count": len(test_cases),
            "endpoint_count": len((spec.get("paths") or {})),
        },
        "bundle": {
            "openapi": spec,
            "features": features,
            "stories": stories,
            "test_cases": test_cases,
        },
    }
