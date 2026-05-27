"""Resolve UserStory.flows on upload (LLM auto-derive or approval queue)."""

from __future__ import annotations

import logging

import config
from services.flow_derivation import derive_flows
from services.flow_proposal_store import create_proposal
from services.llm_client import LLMError

logger = logging.getLogger(__name__)


def prepare_story_flows(story: dict) -> tuple[dict, dict]:
    """
    Mutates story['flows'] when appropriate.
    Returns (story, meta) with flow_derivation / needs_proposal / proposal_id.
    """
    meta: dict = {}

    if story.get("flows"):
        meta["flow_derivation"] = {"source": "file"}
        return story, meta

    story["flows"] = derive_flows(story)
    meta["flow_derivation"] = story.pop(
        "_flow_derivation",
        {"source": "llm" if config.USE_LLM_FLOWS and config.OPENAI_API_KEY else "heuristic"},
    )
    return story, meta


def proposal_after_save(story_id: str) -> dict:
    """Create LLM proposal after story exists in Neo4j (FLOW_REQUIRE_APPROVAL path)."""
    try:
        proposal = create_proposal(story_id, mode="full", trigger="story_upload")
    except (LLMError, ValueError) as e:
        logger.warning("Flow proposal failed for %s: %s", story_id, e)
        return {
            "flow_derivation": {
                "source": "heuristic_fallback",
                "pending_approval": False,
                "error": str(e),
            },
            "proposed_flows": [],
            "proposal_error": str(e),
        }
    return {
        "proposal_id": proposal["proposal_id"],
        "flow_derivation": {
            "source": "llm_proposal",
            "pending_approval": True,
            **(proposal.get("llm") or {}),
        },
        "proposed_flows": proposal["proposed_flows"],
    }
