"""Export a story-version subgraph as a knowledge_graph_bundle YAML (re-uploadable)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import yaml

from services import graph_service as gs
from services.linking_engine import _flow_depends_pairs, _flow_feature_node_ids


def _parse_json_field(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _endpoint_to_operation(props: dict) -> dict:
    method = (props.get("method") or "GET").lower()
    op: dict[str, Any] = {}
    if props.get("summary"):
        op["summary"] = props["summary"]

    req_schema = _parse_json_field(props.get("request_schema"))
    if isinstance(req_schema, dict) and req_schema:
        if method == "get" and req_schema.get("properties"):
            op["parameters"] = [
                {
                    "name": name,
                    "in": "query",
                    "schema": prop if isinstance(prop, dict) else {"type": "string"},
                    "required": name in (req_schema.get("required") or []),
                }
                for name, prop in (req_schema.get("properties") or {}).items()
            ]
        else:
            op["requestBody"] = {
                "content": {
                    "application/json": {
                        "schema": req_schema,
                    }
                }
            }

    op["responses"] = {"200": {"description": "Success"}}
    if method == "post":
        op["responses"]["401"] = {"description": "Unauthorized"}
    return op


def _tc_prerequisites(session, tc_node_ids: list[str]) -> dict[str, list[str]]:
    if not tc_node_ids:
        return {}
    rows = session.run(
        """
        MATCH (tc:TestCase)-[:DEPENDS_ON]->(pre:TestCase)
        WHERE tc.node_id IN $ids
        RETURN tc.base_id AS tc_id, collect(DISTINCT pre.base_id) AS prereqs
        """,
        ids=tc_node_ids,
    )
    out: dict[str, list[str]] = {}
    for row in rows:
        bid = row["tc_id"]
        if bid:
            out[bid] = sorted(row["prereqs"] or [])
    return out


def export_story_bundle(story_node_id: str) -> dict:
    """Build bundle dict for one UserStory version (matches upload format)."""
    story_version = gs.get_user_story_version(story_node_id)
    if not story_version:
        raise ValueError(f"UserStory node '{story_node_id}' not found")

    props = gs.get_node_props(story_node_id) or {}
    flows = [str(f).strip() for f in (props.get("flows") or []) if str(f).strip()]
    story_base_id = props.get("base_id") or story_version.get("base_id")
    version = int(props.get("version") or story_version.get("version") or 1)

    subgraph = gs.get_story_subgraph(story_node_id)
    nodes_by_id = {n["id"]: n for n in subgraph.get("nodes") or []}

    flow_feature_ids = _flow_feature_node_ids(flows)
    flow_depends = _flow_depends_pairs(flows)
    feat_id_to_name = {
        nid: (nodes_by_id[nid]["properties"].get("name") or nodes_by_id[nid]["base_id"])
        for nid in flow_feature_ids
        if nid in nodes_by_id
    }
    feat_name_to_depends: dict[str, list[str]] = {name: [] for name in feat_id_to_name.values()}
    for dep_id, pre_id in flow_depends:
        dep_name = feat_id_to_name.get(dep_id)
        pre_name = feat_id_to_name.get(pre_id)
        if dep_name and pre_name and pre_name not in feat_name_to_depends.get(dep_name, []):
            feat_name_to_depends.setdefault(dep_name, []).append(pre_name)

    feature_nodes_by_name: dict[str, dict] = {}
    for node in subgraph.get("nodes") or []:
        if node.get("type") != "Feature":
            continue
        p = node.get("properties") or {}
        name = (p.get("name") or node.get("base_id") or "").strip()
        if name:
            feature_nodes_by_name[name] = node

    feature_to_apis: dict[str, list[str]] = {}
    for edge in subgraph.get("edges") or []:
        if edge.get("rel_type") != "USES_API":
            continue
        src = nodes_by_id.get(edge["source"])
        tgt = nodes_by_id.get(edge["target"])
        if not src or not tgt or src.get("type") != "Feature" or tgt.get("type") != "APIEndpoint":
            continue
        fname = (src.get("properties") or {}).get("name") or src.get("base_id") or ""
        path = (tgt.get("properties") or {}).get("path")
        if fname and path and path not in feature_to_apis.get(fname, []):
            feature_to_apis.setdefault(fname, []).append(path)

    features_out: list[dict] = []
    seen_features: set[str] = set()
    for fname in flows:
        node = feature_nodes_by_name.get(fname)
        if not node:
            continue
        p = node.get("properties") or {}
        name = (p.get("name") or fname).strip()
        if name in seen_features:
            continue
        seen_features.add(name)
        apis_used = list(feature_to_apis.get(name) or p.get("apis_used") or [])
        depends_on = list(p.get("depends_on") or feat_name_to_depends.get(name) or [])
        features_out.append({
            "name": name,
            "description": p.get("description") or "",
            "apis_used": apis_used,
            "depends_on": depends_on,
        })

    openapi_paths: dict[str, dict] = {}
    for node in subgraph.get("nodes") or []:
        if node.get("type") != "APIEndpoint":
            continue
        p = node.get("properties") or {}
        path = p.get("path")
        method = (p.get("method") or "GET").lower()
        if not path:
            continue
        openapi_paths.setdefault(path, {})[method] = _endpoint_to_operation(p)

    tc_nodes = [n for n in (subgraph.get("nodes") or []) if n.get("type") == "TestCase"]
    tc_node_ids = [n["id"] for n in tc_nodes]

    with gs._get_driver().session() as session:
        tc_prereqs = _tc_prerequisites(session, tc_node_ids)

    test_cases_out: list[dict] = []
    for node in tc_nodes:
        p = node.get("properties") or {}
        tc_id = p.get("base_id") or node.get("base_id")
        if not tc_id:
            continue
        linked = p.get("linked_to") or ""
        if linked and linked == story_base_id:
            linked = props.get("title") or story_base_id
        steps = _parse_json_field(p.get("steps")) or []
        if isinstance(steps, str):
            steps = [steps]
        item = {
            "tc_id": tc_id,
            "linked_to": linked,
            "title": p.get("title") or tc_id,
            "type": p.get("type") or "positive",
            "test_layer": p.get("test_layer") or "api",
            "steps": steps,
            "expected_result": p.get("expected_result") or "",
        }
        prereqs = tc_prereqs.get(tc_id) or []
        if prereqs:
            item["depends_on_test_cases"] = prereqs
        test_cases_out.append(item)

    title = props.get("title") or "Plan Change"
    bundle = {
        "kind": "knowledge_graph_bundle",
        "title": f"{title} — v{version} export",
        "export_meta": {
            "story_id": story_base_id,
            "version": version,
            "story_node_id": story_node_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "flows": flows,
        },
        "openapi": {
            "openapi": "3.0.0",
            "info": {
                "title": f"{title} API",
                "version": str(version),
            },
            "paths": openapi_paths,
        },
        "features": features_out,
        "test_cases": test_cases_out,
        "user_story": {
            "title": title,
            "content": props.get("content") or "",
            "flows": flows,
            "depends_on": list(props.get("depends_on") or []),
            "blocked_by": list(props.get("blocked_by") or []),
        },
    }
    return bundle


def export_story_bundle_yaml(story_node_id: str) -> tuple[str, str]:
    """Return (yaml_text, suggested_filename)."""
    bundle = export_story_bundle(story_node_id)
    meta = bundle.pop("export_meta", {})
    story_id = meta.get("story_id") or "story"
    version = meta.get("version") or 1
    filename = f"{story_id}_v{version}.yaml"

    yaml_text = yaml.dump(
        bundle,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )
    return yaml_text, filename
