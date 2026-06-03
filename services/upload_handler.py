"""Process file uploads — schema v2."""

import logging

import config
from services import graph_service as gs

logger = logging.getLogger(__name__)
from services import linking_engine as mapper
from services.content_hash import hash_bytes
from services.duplicate_check import check_parsed_upload
from services.entity_identity import resolve_upload_items
from services.bundle_ingest import process_bundle
from services.bundle_parser import ENTITY_BUNDLE
from services.openapi_ingest import ingest_openapi
from services.story_flows import prepare_story_flows, proposal_after_save
from services.story_flow_delta import compute_story_flow_delta
from services.upload_errors import DuplicateUploadError
from services.upload_version import resolve_upload_version_policy


def _graph_for_upload_response(
    *,
    entity_type: str,
    node_id: str | None,
    story_id: str | None,
) -> dict | None:
    if not config.UPLOAD_RETURN_GRAPH:
        return None
    if entity_type == "user_story" and node_id:
        return gs.get_story_subgraph(node_id)
    if story_id:
        return gs.get_full_graph(story_base_id=story_id)
    return gs.get_full_graph()


def process_upload(
    parsed: dict,
    story_id: str | None = None,
    *,
    raw_bytes: bytes | None = None,
    allow_duplicate: bool = False,
    version_policy: str = "replace",
    filename: str | None = None,
    **_,
) -> dict:
    entity_type = parsed["entity_type"]
    items = parsed["items"]

    identity_meta: list[dict] = []
    if entity_type not in ("api_spec", ENTITY_BUNDLE):
        items, identity_meta = resolve_upload_items(entity_type, items)
        parsed["items"] = items

    if not allow_duplicate and entity_type != ENTITY_BUNDLE:
        dupes = check_parsed_upload(parsed, raw_bytes=raw_bytes)
        if dupes:
            raise DuplicateUploadError(dupes)

    if entity_type in ("api_spec", ENTITY_BUNDLE):
        effective_version_policy = "replace"
    else:
        effective_version_policy = resolve_upload_version_policy(
            parsed, identity_meta, raw_bytes=raw_bytes
        )

    edges_total = []
    last_node_id = None
    sid = story_id

    if entity_type == ENTITY_BUNDLE:
        bundle = parsed["bundle"]
        result = process_bundle(bundle, version_policy=effective_version_policy)
        sid = result.get("story_id") or story_id
        return {
            **result,
            "graph": _graph_for_upload_response(
                entity_type="user_story",
                node_id=result.get("node_id"),
                story_id=sid,
            ),
            "identity": [],
        }

    if entity_type == "api_spec":
        bundle_hash = hash_bytes(raw_bytes) if raw_bytes else None
        for item in items:
            ingested = ingest_openapi(
                item["spec"],
                openapi_bundle_hash=bundle_hash,
                save_response_schemas=False,
            )
            if ingested["endpoints"]:
                last_node_id = ingested["endpoints"][-1]["node_id"]
        edges_total = mapper.resync_graph(full=True)
        return {
            "success": True,
            "entity_type": entity_type,
            "count": len(items),
            "edges_created": edges_total,
            "node_id": last_node_id,
            "graph": _graph_for_upload_response(
                entity_type=entity_type, node_id=last_node_id, story_id=story_id
            ),
            "message": "OpenAPI ingested (endpoints only; response schemas not in graph)",
        }

    flow_meta: dict = {}
    story_flow_delta: dict | None = None
    base_id = None
    last_version = None
    for item in items:
        if entity_type == "user_story":
            item, flow_meta = prepare_story_flows(item)
            r = gs.save_user_story(item, version_policy=effective_version_policy)
            if flow_meta.get("needs_proposal"):
                try:
                    flow_meta.update(proposal_after_save(item["story_id"]))
                except Exception as e:
                    logger.warning("proposal_after_save failed: %s", e)
                    flow_meta["proposal_error"] = str(e)
            sid = item["story_id"]
            base_id = item["story_id"]
        elif entity_type == "feature":
            r = gs.save_feature(item, version_policy=effective_version_policy)
            base_id = item["feature_id"]
        elif entity_type == "api_endpoint":
            r = gs.save_endpoint(item, version_policy=effective_version_policy)
            base_id = r["base_id"]
        elif entity_type == "test_case":
            if item.get("flow_id") and not item.get("linked_to"):
                item["linked_to"] = item["flow_id"]
            r = gs.save_test_case(item, version_policy=effective_version_policy)
            base_id = item["tc_id"]
        else:
            raise ValueError(f"Unsupported: {entity_type}")

        last_node_id = r["node_id"]
        last_version = r.get("version")

    sync_warnings: list[str] = []
    try:
        if entity_type == "user_story" and base_id:
            edges_total = mapper.resync_graph(story_base_ids=[base_id])
        elif entity_type == "feature" and base_id:
            feat = gs.get_feature(base_id)
            story_ids = mapper.find_story_base_ids_for_feature(feat) if feat else []
            edges_total = mapper.resync_graph(
                story_base_ids=story_ids,
                feature_base_ids=[base_id],
            )
            if effective_version_policy == "deprecate":
                deprecated_tcs = gs.deprecate_test_cases_for_linked_entity("feature", base_id)
                if deprecated_tcs:
                    sync_warnings.append(
                        f"Deprecated linked test cases for feature {base_id}: {', '.join(sorted(deprecated_tcs))}"
                    )
                    mapper.resync_graph(full=True)
        elif entity_type == "test_case" and base_id:
            edges_total = mapper.resync_graph(test_case_base_ids=[base_id])
        elif entity_type == "api_endpoint" and base_id:
            edges_total = mapper.resync_graph(full=True)
            if effective_version_policy == "deprecate":
                deprecated_tcs = gs.deprecate_test_cases_for_linked_entity("api_endpoint", base_id)
                if deprecated_tcs:
                    sync_warnings.append(
                        f"Deprecated linked test cases for endpoint {base_id}: {', '.join(sorted(deprecated_tcs))}"
                    )
                    mapper.resync_graph(full=True)
        else:
            edges_total = mapper.resync_graph(full=True)
    except Exception as e:
        logger.exception("resync_graph failed after upload")
        sync_warnings.append(f"Re-link skipped: {e}")
        edges_total = []

    if entity_type == "user_story" and base_id and effective_version_policy == "deprecate":
        try:
            story_flow_delta = compute_story_flow_delta(base_id)
        except Exception as e:
            logger.warning("story flow delta failed: %s", e)

    out = {
        "success": True,
        "entity_type": entity_type,
        "count": len(items),
        "node_id": last_node_id,
        "base_id": base_id,
        "edges_created": edges_total,
        "message": f"Uploaded {len(items)} item(s)",
        "graph": _graph_for_upload_response(
            entity_type=entity_type,
            node_id=last_node_id,
            story_id=sid or base_id,
        ),
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
    if entity_type == "user_story" and base_id:
        history = gs.get_user_story_history(base_id)
        if len(history) >= 2:
            try:
                from services import impact_analyser

                impact = impact_analyser.analyse("user_story", base_id)
                if impact:
                    out["impact"] = impact
            except Exception as e:
                logger.warning("impact analysis failed: %s", e)

    if story_flow_delta:
        out["story_flow_delta"] = story_flow_delta
        if story_flow_delta.get("has_changes"):
            add_n = ", ".join(f["name"] for f in story_flow_delta.get("added", []))
            rem_n = ", ".join(f["name"] for f in story_flow_delta.get("removed", []))
            mod_n = ", ".join(f["name"] for f in story_flow_delta.get("modified", []))
            parts = []
            if add_n:
                parts.append(f"added: {add_n}")
            if mod_n:
                parts.append(f"modified: {mod_n}")
            if rem_n:
                parts.append(f"removed: {rem_n}")
            if parts:
                out["message"] = f"{out['message']} ({'; '.join(parts)})"
    try:
        from services import tracking

        tracking.on_upload(
            entity_type,
            base_id=base_id,
            node_id=last_node_id,
            version=last_version,
            filename=filename,
            version_policy=effective_version_policy,
            identity_meta=identity_meta,
            extra={"flow_meta": flow_meta} if flow_meta else None,
        )
    except Exception as e:
        logger.warning("Upload tracking skipped: %s", e)
    return out
