"""Process file uploads — schema v2."""

import logging

from services import graph_service as gs

logger = logging.getLogger(__name__)
from services import linking_engine as mapper
from services.content_hash import hash_bytes
from services.duplicate_check import check_parsed_upload
from services.entity_identity import resolve_upload_items
from services.openapi_ingest import ingest_openapi
from services.story_flows import prepare_story_flows, proposal_after_save
from services.upload_errors import DuplicateUploadError


def process_upload(
    parsed: dict,
    story_id: str | None = None,
    *,
    raw_bytes: bytes | None = None,
    allow_duplicate: bool = False,
    version_policy: str = "deprecate",
    filename: str | None = None,
    **_,
) -> dict:
    entity_type = parsed["entity_type"]
    items = parsed["items"]

    identity_meta: list[dict] = []
    if entity_type != "api_spec":
        items, identity_meta = resolve_upload_items(entity_type, items)
        parsed["items"] = items

    if not allow_duplicate:
        dupes = check_parsed_upload(parsed, raw_bytes=raw_bytes)
        if dupes:
            raise DuplicateUploadError(dupes)
    edges_total = []
    last_node_id = None
    sid = story_id

    if entity_type == "api_spec":
        bundle_hash = hash_bytes(raw_bytes) if raw_bytes else None
        for item in items:
            ingested = ingest_openapi(item["spec"], openapi_bundle_hash=bundle_hash)
            if ingested["endpoints"]:
                last_node_id = ingested["endpoints"][-1]["node_id"]
        edges_total = mapper.resync_graph()
        return {
            "success": True,
            "entity_type": entity_type,
            "count": len(items),
            "edges_created": edges_total,
            "node_id": last_node_id,
            "graph": gs.get_full_graph(),
            "message": "OpenAPI ingested",
        }

    flow_meta: dict = {}
    base_id = None
    last_version = None
    for item in items:
        if entity_type == "user_story":
            item, flow_meta = prepare_story_flows(item)
            r = gs.save_user_story(item, version_policy=version_policy)
            if flow_meta.get("needs_proposal"):
                try:
                    flow_meta.update(proposal_after_save(item["story_id"]))
                except Exception as e:
                    logger.warning("proposal_after_save failed: %s", e)
                    flow_meta["proposal_error"] = str(e)
            sid = item["story_id"]
            base_id = item["story_id"]
        elif entity_type == "feature":
            r = gs.save_feature(item, version_policy=version_policy)
            base_id = item["feature_id"]
        elif entity_type == "api_endpoint":
            r = gs.save_endpoint(item, version_policy=version_policy)
            base_id = r["base_id"]
        elif entity_type == "test_case":
            if item.get("flow_id") and not item.get("linked_to"):
                item["linked_to"] = item["flow_id"]
            r = gs.save_test_case(item, version_policy=version_policy)
            base_id = item["tc_id"]
        else:
            raise ValueError(f"Unsupported: {entity_type}")

        last_node_id = r["node_id"]
        last_version = r.get("version")

    sync_warnings: list[str] = []
    try:
        edges_total = mapper.resync_graph()
    except Exception as e:
        logger.exception("resync_graph failed after upload")
        sync_warnings.append(f"Re-link skipped: {e}")
        edges_total = []

    out = {
        "success": True,
        "entity_type": entity_type,
        "count": len(items),
        "node_id": last_node_id,
        "base_id": base_id,
        "edges_created": edges_total,
        "message": f"Uploaded {len(items)} item(s)",
        "graph": gs.get_full_graph(),
        "identity": identity_meta,
        "warnings": sync_warnings,
    }
    if sync_warnings:
        out["message"] = f"{out['message']} (with warnings)"
    if flow_meta:
        out["flows"] = items[0].get("flows") if entity_type == "user_story" else None
        out["flow_derivation"] = flow_meta.get("flow_derivation")
        if flow_meta.get("proposal_id"):
            out["proposal_id"] = flow_meta["proposal_id"]
            out["proposed_flows"] = flow_meta.get("proposed_flows")
            out["message"] = "Story saved; flow proposal pending approval"
    try:
        from services import tracking

        tracking.on_upload(
            entity_type,
            base_id=base_id,
            node_id=last_node_id,
            version=last_version,
            filename=filename,
            version_policy=version_policy,
            identity_meta=identity_meta,
            extra={"flow_meta": flow_meta} if flow_meta else None,
        )
    except Exception as e:
        logger.warning("Upload tracking skipped: %s", e)
    return out
