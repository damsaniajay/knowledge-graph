"""Parse OpenAPI specs into APIEndpoint + APIResponseSchema nodes."""

from services import graph_service as gs
from services import linking_engine as linker


def parse_openapi(spec: dict) -> tuple[list[dict], list[dict]]:
    endpoints = []
    response_schemas = []

    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            if not isinstance(operation, dict):
                continue
            req_schema = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            base_id = f"{method.upper()}:{path}"
            endpoints.append({
                "path": path,
                "method": method.upper(),
                "summary": operation.get("summary", ""),
                "request_schema": req_schema,
            })

            for code, resp in (operation.get("responses") or {}).items():
                try:
                    status_code = int(code)
                except ValueError:
                    continue
                content = (resp.get("content") or {}).get("application/json", {})
                schema_body = content.get("schema", {})
                outcome = _outcome_label(status_code, resp.get("description", ""))
                response_schemas.append({
                    "endpoint_id": base_id,
                    "status_code": status_code,
                    "outcome_label": outcome,
                    "schema": schema_body,
                    "description": resp.get("description", ""),
                })
    return endpoints, response_schemas


def _outcome_label(status_code: int, description: str) -> str:
    if status_code < 300:
        return "success"
    if status_code == 400:
        return "validation_error"
    if status_code == 401:
        return "auth_failed"
    if status_code == 404:
        return "not_found"
    if status_code >= 500:
        return "server_error"
    return (description or "outcome")[:40].replace(" ", "_").lower()


def ingest_openapi(spec: dict, *, openapi_bundle_hash: str | None = None) -> dict:
    endpoints, schemas = parse_openapi(spec)
    results = {"endpoints": [], "response_schemas": [], "edges_created": []}

    for ep in endpoints:
        r = gs.save_endpoint(ep, openapi_bundle_hash=openapi_bundle_hash)
        results["endpoints"].append(r)

    for sch in schemas:
        r = gs.save_response_schema(sch)
        results["response_schemas"].append(r)

    results["edges_created"] = linker.resync_graph()
    return results
