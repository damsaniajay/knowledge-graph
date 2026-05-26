"""Flow proposals: LLM extract → approve → commit to UserStory.flows."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import config
from services import graph_service as gs
from services.flow_proposal_store import (
    approve_proposal,
    commit_proposal,
    create_proposal,
    get_proposal,
    list_proposals,
    patch_step,
    reject_proposal,
)

router = APIRouter(prefix="/api/flow-proposals", tags=["flow-proposals"])


class ExtractBody(BaseModel):
    title: str | None = None
    content: str | None = None
    feature_id: str | None = None
    api_base_id: str | None = None


class PatchFlowBody(BaseModel):
    feature_name: str | None = None
    order: list[str] | None = None
    insert_after: str | None = None
    remove: bool = False


class ApproveBody(BaseModel):
    feature_names: list[str] | None = Field(
        default=None, description="Approve subset only; omit for all"
    )


class RejectBody(BaseModel):
    reason: str = ""


@router.post("/extract", status_code=201)
def extract_flows(
    story_id: str = Query(..., description="User story base_id"),
    mode: str = Query("full", description="full or delta"),
    trigger: str = Query("manual"),
    body: ExtractBody | None = None,
):
    if not config.OPENAI_API_KEY:
        raise HTTPException(503, "OPENAI_API_KEY not configured — cannot run LLM extract")

    story = gs.get_user_story(story_id)
    if not story and body and (body.title or body.content):
        story = {
            "story_id": story_id,
            "title": body.title or "",
            "content": body.content or "",
            "flows": [],
        }
    if not story:
        raise HTTPException(404, f"Story '{story_id}' not found")

    current = list(story.get("flows") or [])
    effective_mode = "delta" if current and mode == "delta" else ("delta" if current else "full")

    try:
        proposal = create_proposal(
            story_id,
            mode=effective_mode,
            trigger=trigger,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if not proposal["validation"]["passed"]:
        raise HTTPException(
            422,
            detail={
                "message": "Validation failed",
                "validation": proposal["validation"],
                "proposal_id": proposal["proposal_id"],
            },
        )

    return proposal


@router.get("")
def list_flow_proposals(
    story_id: str | None = None,
    status: str | None = None,
):
    return {"proposals": list_proposals(story_id=story_id, status=status)}


@router.get("/{proposal_id}")
def get_flow_proposal(proposal_id: str):
    p = get_proposal(proposal_id)
    if not p:
        raise HTTPException(404, "Proposal not found")
    return p


@router.patch("/{proposal_id}/flows/{feature_name}")
def edit_proposed_flow(proposal_id: str, feature_name: str, body: PatchFlowBody):
    try:
        p = patch_step(
            proposal_id,
            feature_name,
            body.model_dump(exclude_none=True),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"proposal": p, "edited_by_human": True}


@router.post("/{proposal_id}/approve")
def approve_flow_proposal(proposal_id: str, body: ApproveBody | None = None):
    try:
        names = body.feature_names if body else None
        return approve_proposal(proposal_id, feature_names=names)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{proposal_id}/reject")
def reject_flow_proposal(proposal_id: str, body: RejectBody | None = None):
    try:
        reason = body.reason if body else ""
        return reject_proposal(proposal_id, reason=reason)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{proposal_id}/commit")
def commit_flow_proposal(proposal_id: str, story_id: str | None = Query(None)):
    try:
        result = commit_proposal(proposal_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if story_id:
        result["graph"] = gs.get_full_graph(story_id)
    else:
        result["graph"] = gs.get_full_graph(result["story_id"])
    return result
