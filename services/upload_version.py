"""
Decide when an upload should prompt for version policy (deprecate vs delete).

Normal upload (replace): update the current node in place — no new version row.
Version upload (deprecate): archive current, create v+1 (only after user confirms).
Delete upload: remove all prior versions, store as v1.
"""

from __future__ import annotations

from services import graph_service as gs
from services.content_hash import hash_feature, hash_test_case, hash_upload_item, hash_user_story
from services.duplicate_check import find_duplicate
from services.story_flow_delta import preview_story_upload_delta


def _content_changed(entity_type: str, item: dict, base_id: str) -> bool:
    label_map = {
        "user_story": ("UserStory", hash_user_story),
        "feature": ("Feature", hash_feature),
        "test_case": ("TestCase", hash_test_case),
    }
    spec = label_map.get(entity_type)
    if not spec:
        return True
    label, hash_fn = spec
    new_hash = hash_fn(item)
    old_hash = gs.get_current_content_hash(label, base_id)
    return old_hash is None or new_hash != old_hash


def assess_upload_versioning(
    parsed: dict,
    identity_meta: list[dict],
    *,
    raw_bytes: bytes | None = None,
) -> dict:
    """
    Returns whether the UI should ask deprecate vs delete.

    Only when the upload matches an existing entity AND content/flows actually changed.
    Identical content is handled as duplicate (409), not versioning.
    """
    entity_type = parsed["entity_type"]
    if entity_type == "api_spec":
        return {"needs_version_decision": False}

    meta = identity_meta[0] if identity_meta else {}
    item = parsed["items"][0] if parsed.get("items") else {}

    if find_duplicate(entity_type, item, raw_bytes=raw_bytes):
        return {"needs_version_decision": False, "reason": "duplicate"}

    if meta.get("is_new_entity"):
        return {"needs_version_decision": False, "reason": "new_entity"}

    base_id = meta.get("assigned_id")
    if not base_id:
        return {"needs_version_decision": False, "reason": "no_match"}

    if not meta.get("is_version_update"):
        return {"needs_version_decision": False, "reason": "not_version_update"}

    change_preview: dict = {"has_changes": False}

    if entity_type == "user_story":
        change_preview = preview_story_upload_delta(item, base_id)
        has_changes = bool(change_preview.get("has_changes")) or _content_changed(
            "user_story", item, base_id
        )
    elif entity_type == "feature":
        has_changes = _content_changed("feature", item, base_id)
        change_preview = {
            "has_changes": has_changes,
            "story_id": base_id,
            "change_type": "content" if has_changes else None,
        }
    elif entity_type == "test_case":
        has_changes = _content_changed("test_case", item, base_id)
        change_preview = {"has_changes": has_changes, "change_type": "content" if has_changes else None}
    else:
        has_changes = _content_changed(entity_type, item, base_id) if entity_type else True
        change_preview = {"has_changes": has_changes}

    return {
        "needs_version_decision": has_changes,
        "reason": "content_or_flow_delta" if has_changes else "unchanged",
        "change_preview": change_preview,
        "version_target": {
            "base_id": base_id,
            "entity_type": entity_type,
        },
    }
