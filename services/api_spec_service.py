"""
api_spec_service.py
Parse a Swagger/OpenAPI YAML spec, store endpoints in Neo4j, and detect
schema changes when a new version is uploaded.

Each endpoint is stored as:
  (:APIEndpoint {endpoint_id, api_name, path, method, summary,
                  request_schema, response_schema, version})

Delta detection compares stored endpoint schemas against the new spec and
returns a structured report of added / removed / changed endpoints, plus
which APITestScripts are impacted via the COVERS_ENDPOINT edge.
"""

import json
import yaml

from services import graph_service


def _parse_spec(spec_path: str) -> tuple[str, str, list[dict]]:
    """
    Parse a YAML OpenAPI spec.
    Returns (api_name, version, endpoints_list).
    """
    with open(spec_path, encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    api_name = spec.get("info", {}).get("title", "Unknown API")
    version = spec.get("info", {}).get("version", "1.0.0")
    endpoints = []

    for path, path_item in spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue

            req_schema = _extract_request_schema(operation)
            resp_schema = _extract_response_schema(operation)

            endpoints.append({
                "endpoint_id": f"{method.upper()}:{path}",
                "api_name": api_name,
                "path": path,
                "method": method.upper(),
                "summary": operation.get("summary", ""),
                "request_schema": json.dumps(req_schema),
                "response_schema": json.dumps(resp_schema),
                "version": version,
            })

    return api_name, version, endpoints


def _extract_request_schema(operation: dict) -> dict:
    try:
        return (
            operation.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
    except Exception:
        return {}


def _extract_response_schema(operation: dict) -> dict:
    responses = {}
    for code, resp in operation.get("responses", {}).items():
        try:
            schema = (
                resp.get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            responses[str(code)] = {
                "description": resp.get("description", ""),
                "schema": schema,
            }
        except Exception:
            responses[str(code)] = {"description": str(resp)}
    return responses


# ── Public API ────────────────────────────────────────────────────────────────

def ingest(spec_path: str) -> list[dict]:
    """
    Parse spec and store all endpoints in Neo4j.
    Returns the list of parsed endpoint dicts.
    """
    api_name, version, endpoints = _parse_spec(spec_path)
    for ep in endpoints:
        graph_service.save_endpoint(ep)
    return endpoints


def delta(spec_path: str) -> dict:
    """
    Compare a new spec against endpoints already stored in Neo4j.
    Returns a report:
    {
      "api_name":        str,
      "new_version":     str,
      "added":           [endpoint_id, ...],
      "added_endpoints": [{full endpoint dict}, ...],   # for approve-api
      "removed":         [endpoint_id, ...],
      "changed":         [{
          "endpoint_id":       str,
          "changes":           [str, ...],
          "impacted_scripts":  [{script_id, tc_id}, ...],
          "impacted_tcs":      [tc_id, ...],
          "new_endpoint":      {full endpoint dict},     # for approve-api
      }, ...],
      "unchanged":       [endpoint_id, ...],
    }
    """
    api_name, version, new_endpoints = _parse_spec(spec_path)
    new_by_id = {ep["endpoint_id"]: ep for ep in new_endpoints}

    stored = graph_service.get_endpoints_by_api(api_name)
    stored_by_id = {ep["endpoint_id"]: ep for ep in stored}

    report = {
        "api_name": api_name,
        "new_version": version,
        "added": [],
        "added_endpoints": [],
        "removed": [],
        "changed": [],
        "unchanged": [],
    }

    all_ids = set(new_by_id) | set(stored_by_id)

    for eid in all_ids:
        if eid not in stored_by_id:
            report["added"].append(eid)
            report["added_endpoints"].append(new_by_id[eid])
            continue
        if eid not in new_by_id:
            report["removed"].append(eid)
            continue

        changes = _diff_schemas(stored_by_id[eid], new_by_id[eid])
        if not changes:
            report["unchanged"].append(eid)
        else:
            impacted_scripts = graph_service.get_scripts_for_endpoint(eid)
            impacted_tcs = graph_service.get_api_tcs_for_endpoint(eid)
            report["changed"].append({
                "endpoint_id":      eid,
                "changes":          changes,
                "impacted_scripts": impacted_scripts,
                "impacted_tcs":     impacted_tcs,
                "new_endpoint":     new_by_id[eid],
            })

    return report


def _diff_schemas(stored: dict, new: dict) -> list[str]:
    """
    Compare request/response schemas of two endpoint dicts.
    Returns a list of human-readable change descriptions.
    """
    changes = []

    old_req = json.loads(stored.get("request_schema", "{}"))
    new_req = json.loads(new.get("request_schema", "{}"))

    old_resp = json.loads(stored.get("response_schema", "{}"))
    new_resp = json.loads(new.get("response_schema", "{}"))

    # Check required fields added/removed
    old_required = set(old_req.get("required", []))
    new_required = set(new_req.get("required", []))

    for field in new_required - old_required:
        changes.append(f"New mandatory request field added: '{field}'")
    for field in old_required - new_required:
        changes.append(f"Mandatory request field removed: '{field}'")

    # Check properties added/removed
    old_props = set(old_req.get("properties", {}).keys())
    new_props = set(new_req.get("properties", {}).keys())

    for prop in new_props - old_props:
        changes.append(f"New request field added: '{prop}'")
    for prop in old_props - new_props:
        changes.append(f"Request field removed: '{prop}'")

    # Check response codes added/removed
    old_codes = set(old_resp.keys())
    new_codes = set(new_resp.keys())

    for code in new_codes - old_codes:
        desc = new_resp[code].get("description", "")
        changes.append(f"New response code added: {code} ({desc})")
    for code in old_codes - new_codes:
        changes.append(f"Response code removed: {code}")

    # Check enum values changed (e.g. payment_method gains bnpl)
    old_enum_map = _extract_enums(old_req)
    new_enum_map = _extract_enums(new_req)

    for field, new_vals in new_enum_map.items():
        old_vals = set(old_enum_map.get(field, []))
        added_vals = set(new_vals) - old_vals
        removed_vals = old_vals - set(new_vals)
        if added_vals:
            changes.append(f"Enum '{field}' gained values: {sorted(added_vals)}")
        if removed_vals:
            changes.append(f"Enum '{field}' lost values: {sorted(removed_vals)}")

    return changes


def _extract_enums(schema: dict) -> dict[str, list]:
    """Walk properties and collect any enum definitions."""
    result = {}
    for prop, defn in schema.get("properties", {}).items():
        if "enum" in defn:
            result[prop] = defn["enum"]
    return result
