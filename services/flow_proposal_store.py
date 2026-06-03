"""In-memory flow proposals (LLM extract → human approve → commit to UserStory.flows)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from services import graph_service as gs
from services import linking_engine as mapper
from services.flow_derivation import build_flow_steps, derive_flows, derive_flows_llm
from services.llm_client import LLMError

_proposals: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_proposal(story_id: str, proposed_flows: list[str]) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    story = gs.get_user_story(story_id)
    if not story:
        errors.append("story not found")
    if not proposed_flows:
        errors.append("empty flows list")
    features = gs.get_all_features()
    catalog = {f.get("name") or f.get("base_id") for f in features}
    if not catalog:
        warnings.append("no features in graph yet — commit after uploading features")
    for step in proposed_flows:
        if catalog and step not in catalog:
            warnings.append(f"feature not in graph yet: {step}")
    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


def create_proposal(
    story_id: str,
    *,
    mode: str = "full",
    trigger: str = "manual",
    proposed_flows: list[str] | None = None,
    llm_meta: dict | None = None,
) -> dict:
    story = gs.get_user_story(story_id)
    if not story:
        raise ValueError(f"Story '{story_id}' not found")

    current = list(story.get("flows") or [])
    if proposed_flows is None:
        try:
            if mode == "delta" and current:
                llm_out = derive_flows_llm(story, current_flows=current)
            else:
                llm_out = derive_flows_llm(
                    story, current_flows=current if mode == "delta" else None
                )
            proposed_flows = llm_out["flows"]
            llm_meta = {
                "confidence": llm_out.get("confidence"),
                "evidence": llm_out.get("evidence"),
                "source": "llm",
            }
        except LLMError as e:
            proposed_flows = derive_flows(
                story,
                current_flows=current if mode == "delta" else None,
                force=bool(current),
            )
            llm_meta = {
                "source": "heuristic_fallback",
                "error": str(e),
                **(story.pop("_flow_derivation", {}) or {}),
            }
    else:
        llm_meta = llm_meta or {}

    validation = _validate_proposal(story_id, proposed_flows)
    steps = build_flow_steps(proposed_flows, current)
    unchanged = [s["feature_name"] for s in steps if s["action"] == "unchanged"]

    proposal_id = f"prop-{uuid.uuid4().hex[:8]}"
    record = {
        "proposal_id": proposal_id,
        "story_id": story_id,
        "mode": mode,
        "trigger": trigger,
        "status": "pending_approval",
        "proposed_flows": proposed_flows,
        "current_flows": current,
        "flows": steps,
        "unchanged_flow_ids": unchanged,
        "validation": validation,
        "llm": llm_meta,
        "created_at": _now(),
        "approved_at": None,
        "committed_at": None,
    }
    _proposals[proposal_id] = record
    return record


def list_proposals(
    story_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    out = []
    for p in _proposals.values():
        if story_id and p["story_id"] != story_id:
            continue
        if status and p["status"] != status:
            continue
        out.append(
            {
                "proposal_id": p["proposal_id"],
                "story_id": p["story_id"],
                "mode": p["mode"],
                "status": p["status"],
                "flow_count": len(p["proposed_flows"]),
                "created_at": p["created_at"],
            }
        )
    return sorted(out, key=lambda x: x["created_at"], reverse=True)


def get_proposal(proposal_id: str) -> dict | None:
    p = _proposals.get(proposal_id)
    if not p:
        return None
    return {
        **p,
        "diff": {
            "added": [x for x in p["proposed_flows"] if x not in p["current_flows"]],
            "removed": [x for x in p["current_flows"] if x not in p["proposed_flows"]],
            "proposed_order": p["proposed_flows"],
            "current_order": p["current_flows"],
        },
    }


def patch_step(proposal_id: str, feature_name: str, patch: dict) -> dict:
    p = _proposals.get(proposal_id)
    if not p:
        raise ValueError("proposal not found")
    if p["status"] not in ("pending_approval", "approved"):
        raise ValueError(f"cannot edit proposal in status {p['status']}")

    flows = list(p["proposed_flows"])
    if "order" in patch and isinstance(patch["order"], list):
        p["proposed_flows"] = [str(x) for x in patch["order"]]
    elif "remove" in patch and patch["remove"]:
        p["proposed_flows"] = [f for f in flows if f != feature_name]
    elif "insert_after" in patch:
        ref = patch["insert_after"]
        new_name = patch.get("feature_name", feature_name)
        if ref in flows:
            idx = flows.index(ref) + 1
            flows.insert(idx, new_name)
            p["proposed_flows"] = flows
    else:
        for i, f in enumerate(flows):
            if f == feature_name:
                flows[i] = patch.get("feature_name", feature_name)
                break
        p["proposed_flows"] = flows

    p["flows"] = build_flow_steps(p["proposed_flows"], p["current_flows"])
    p["edited_by_human"] = True
    p["validation"] = _validate_proposal(p["story_id"], p["proposed_flows"])
    return p


def approve_proposal(proposal_id: str, feature_names: list[str] | None = None) -> dict:
    p = _proposals.get(proposal_id)
    if not p:
        raise ValueError("proposal not found")
    if p["status"] == "committed":
        raise ValueError("already committed")
    if p["status"] == "rejected":
        raise ValueError("proposal was rejected")

    if feature_names:
        p["proposed_flows"] = [f for f in p["proposed_flows"] if f in feature_names]
        p["flows"] = build_flow_steps(p["proposed_flows"], p["current_flows"])

    p["status"] = "approved"
    p["approved_at"] = _now()
    return p


def reject_proposal(proposal_id: str, reason: str = "") -> dict:
    p = _proposals.get(proposal_id)
    if not p:
        raise ValueError("proposal not found")
    p["status"] = "rejected"
    p["reject_reason"] = reason
    return p


def commit_proposal(proposal_id: str) -> dict:
    p = _proposals.get(proposal_id)
    if not p:
        raise ValueError("proposal not found")
    if p["status"] == "committed":
        raise ValueError("already committed")
    if p["status"] == "rejected":
        raise ValueError("proposal was rejected")

    if p["status"] == "pending_approval":
        approve_proposal(proposal_id)

    if not p["validation"]["passed"]:
        raise ValueError(f"validation failed: {p['validation']['errors']}")

    story = gs.get_user_story(p["story_id"])
    if not story:
        raise ValueError("story not found")

    payload = {
        "story_id": p["story_id"],
        "title": story.get("title", ""),
        "content": story.get("content", ""),
        "flows": p["proposed_flows"],
        "depends_on": story.get("depends_on") or [],
        "blocked_by": story.get("blocked_by") or [],
    }
    result = gs.save_user_story(payload)
    mapped = {"edges_created": mapper.resync_graph()}

    p["status"] = "committed"
    p["committed_at"] = _now()
    return {
        "proposal_id": proposal_id,
        "story_id": p["story_id"],
        "flows": p["proposed_flows"],
        "node_id": result["node_id"],
        "version": result["version"],
        "edges_created": mapped["edges_created"],
    }


def apply_flows_to_story(story: dict, *, require_approval: bool) -> dict:
    """
    Used on upload: derive flows and optionally queue approval instead of saving flows.
    Returns {flows, proposal_id?, derivation}.
    """
    meta: dict = {}
    if story.get("flows"):
        return {"flows": story["flows"], "derivation": {"source": "file"}}

    current_story = gs.get_user_story(story["story_id"])
    current_flows = list((current_story or {}).get("flows") or [])

    if require_approval:
        mode = "delta" if current_flows else "full"
        proposal = create_proposal(story["story_id"], mode=mode, trigger="story_upload")
        return {
            "flows": current_flows,
            "proposal_id": proposal["proposal_id"],
            "derivation": {"source": "llm_proposal", "pending": True, **proposal.get("llm", {})},
        }

    flows = derive_flows(
        story,
        current_flows=current_flows if current_flows else None,
        force=bool(current_story),
    )
    meta = story.pop("_flow_derivation", {})
    return {"flows": flows, "derivation": meta}
