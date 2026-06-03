"""File upload — schema v2."""

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from services.duplicate_check import check_parsed_upload
from services.entity_identity import resolve_upload_items
from services.file_parser import ENTITY_API_SPEC, ENTITY_FEATURE, ENTITY_STORY, ENTITY_TEST_CASE, parse_upload
from services.upload_errors import DuplicateUploadError
from services.upload_handler import process_upload
from services.upload_version import assess_upload_versioning

router = APIRouter(prefix="/api/upload", tags=["upload"])

VALID_TYPES = {
    "auto",
    ENTITY_STORY,
    ENTITY_FEATURE,
    ENTITY_TEST_CASE,
    ENTITY_API_SPEC,
    "api_endpoint",
    "bundle",
}
def _duplicate_detail(duplicates: list[dict]) -> dict:
    return {"code": "duplicate", "duplicates": duplicates}


@router.post("/preview")
async def preview_upload(
    file: UploadFile = File(...),
    entity_type: str = Query("auto"),
):
    if entity_type not in VALID_TYPES:
        raise HTTPException(400, f"entity_type must be one of: {', '.join(sorted(VALID_TYPES))}")
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    try:
        parsed = parse_upload(file.filename or "upload.json", content, None if entity_type == "auto" else entity_type)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    identity: list[dict] = []
    if parsed["entity_type"] not in (ENTITY_API_SPEC, "bundle"):
        items, identity = resolve_upload_items(parsed["entity_type"], parsed["items"])
        parsed["items"] = items
        preview = dict(parsed.get("preview") or {})
        item = items[0] if items else {}
        id_key = {"user_story": "story_id", "feature": "feature_id", "test_case": "tc_id"}.get(
            parsed["entity_type"]
        )
        if id_key and item.get(id_key):
            preview["assigned_id"] = item[id_key]
        parsed["preview"] = preview

    duplicates = [] if parsed["entity_type"] == "bundle" else check_parsed_upload(parsed, raw_bytes=content)
    assessment = (
        {"needs_version_decision": False}
        if parsed["entity_type"] == "bundle"
        else assess_upload_versioning(parsed, identity, raw_bytes=content)
    )
    impact_preview = None
    if (
        parsed["entity_type"] == ENTITY_STORY
        and assessment.get("will_create_version")
        and parsed.get("items")
    ):
        from services import impact_analyser

        base_id = (identity[0] if identity else {}).get("assigned_id") or parsed["items"][0].get(
            "story_id"
        )
        if base_id:
            try:
                impact_preview = impact_analyser.preview_user_story_impact(
                    parsed["items"][0], base_id
                )
            except Exception:
                impact_preview = None

    return {
        "filename": file.filename,
        "entity_type": parsed["entity_type"],
        "item_count": len(parsed["items"]),
        "preview": parsed["preview"],
        "valid": True,
        "has_duplicates": bool(duplicates),
        "duplicates": duplicates,
        "identity": identity,
        "needs_version_decision": False,
        "will_create_version": assessment.get("will_create_version", False),
        "change_preview": assessment.get("change_preview"),
        "version_target": assessment.get("version_target"),
        "impact_preview": impact_preview,
    }


@router.post("")
async def upload_file(
    file: UploadFile = File(...),
    entity_type: str = Query("auto"),
    story_id: str | None = Query(None),
    force: bool = Query(False, description="Upload anyway even if identical content exists"),
):
    if entity_type not in VALID_TYPES:
        raise HTTPException(400, f"entity_type must be one of: {', '.join(sorted(VALID_TYPES))}")
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    try:
        parsed = parse_upload(file.filename or "upload.json", content, None if entity_type == "auto" else entity_type)
        result = process_upload(
            parsed,
            story_id=story_id,
            raw_bytes=content,
            allow_duplicate=force,
            filename=file.filename,
        )
    except DuplicateUploadError as e:
        raise HTTPException(409, detail=_duplicate_detail(e.duplicates)) from e
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    result["filename"] = file.filename
    result["preview"] = parsed.get("preview")
    return result
