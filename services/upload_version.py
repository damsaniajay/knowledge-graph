"""
Upload versioning — automatic only (no deprecate/delete UI).

- New entity: first version (replace path).
- Same content: duplicate check blocks upload.
- Changed content on existing entity: archive current, create v+1 (version history kept).
- Hard-delete of prior versions is never used.
"""

from __future__ import annotations

from services import graph_service as gs
from services.content_hash import hash_feature, hash_test_case, hash_user_story
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


def _upload_has_changes(
    entity_type: str,
    item: dict,
    base_id: str,
    *,
    raw_bytes: bytes | None = None,
) -> bool:
    if find_duplicate(entity_type, item, raw_bytes=raw_bytes):
        return False
    if entity_type == "user_story":
        change_preview = preview_story_upload_delta(item, base_id)
        return bool(change_preview.get("has_changes")) or _content_changed(
            "user_story", item, base_id
        )
    if entity_type in ("feature", "test_case"):
        return _content_changed(entity_type, item, base_id)
    return True


def resolve_upload_version_policy(
    parsed: dict,
    identity_meta: list[dict],
    *,
    raw_bytes: bytes | None = None,
) -> str:
    """
    Server-side version policy for file uploads.

    Returns 'deprecate' (archive + v+1) when an existing entity's content changed,
    otherwise 'replace' (first version or in-place — duplicates blocked earlier).
    """
    entity_type = parsed["entity_type"]
    if entity_type in ("api_spec", "bundle"):
        return "replace"

    meta = identity_meta[0] if identity_meta else {}
    item = parsed["items"][0] if parsed.get("items") else {}

    if meta.get("is_new_entity") or not meta.get("is_version_update"):
        return "replace"

    base_id = meta.get("assigned_id")
    if not base_id:
        return "replace"

    if _upload_has_changes(entity_type, item, base_id, raw_bytes=raw_bytes):
        return "deprecate"
    return "replace"


def assess_upload_versioning(
    parsed: dict,
    identity_meta: list[dict],
    *,
    raw_bytes: bytes | None = None,
) -> dict:
    """
    Preview metadata for uploads. Never prompts for deprecate vs delete.
    """
    entity_type = parsed["entity_type"]
    if entity_type == "api_spec":
        return {"needs_version_decision": False}

    meta = identity_meta[0] if identity_meta else {}
    item = parsed["items"][0] if parsed.get("items") else {}

    if find_duplicate(entity_type, item, raw_bytes=raw_bytes):
        return {"needs_version_decision": False, "reason": "duplicate"}

    if meta.get("is_new_entity"):
        return {
            "needs_version_decision": False,
            "reason": "new_entity",
            "will_create_version": False,
        }

    base_id = meta.get("assigned_id")
    if not base_id or not meta.get("is_version_update"):
        return {"needs_version_decision": False, "reason": "no_match", "will_create_version": False}

    change_preview: dict = {"has_changes": False}
    has_changes = False

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
        "needs_version_decision": False,
        "will_create_version": has_changes,
        "reason": "content_or_flow_delta" if has_changes else "unchanged",
        "change_preview": change_preview,
        "version_target": {
            "base_id": base_id,
            "entity_type": entity_type,
        },
    }
