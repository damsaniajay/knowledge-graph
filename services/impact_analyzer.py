"""
impact_analyzer.py
Graph traversal + LLM scoring to decide what needs to change when a story delta is detected.

Decision matrix per TestCase:
  UNCHANGED        — flow hash identical, TC still valid
  UPDATE_REQUIRED  — flow changed, but TC is likely still relevant (needs minor edits)
  REGENERATE       — flow changed substantially, TC should be regenerated from scratch
  OBSOLETE         — flow was removed, TC has no parent flow

For NEW flows (added), all TCs are marked NEW_REQUIRED (they don't exist yet).
"""

import json
import config

from components.common.llm_client import LLMClient
from services import graph_service

_llm = LLMClient(api_key=config.OPENAI_API_KEY, model=config.LLM_MODEL)

_SCORE_SYSTEM = "You are a QA architect assessing the impact of a story change on existing test cases."

_SCORE_USER = """A functional flow has changed. Decide which test cases are still valid.

ORIGINAL FLOW:
{old_flow}

UPDATED FLOW:
{new_flow}

EXISTING TEST CASES FOR THIS FLOW:
{test_cases}

For each test case, assign one of:
  UPDATE_REQUIRED  — TC is still relevant but needs minor edits to match the new flow
  REGENERATE       — TC is no longer accurate; regenerate it from scratch
  UNCHANGED        — TC is still fully valid as-is

Return ONLY a JSON array — no other text:
[
  {{"tc_id": "TC-f1-001", "decision": "UPDATE_REQUIRED", "reason": "one-line reason"}},
  ...
]
"""


def analyze(delta_report: dict, feature_name: str) -> dict:
    """
    Given a DeltaReport from FlowMatcher and the feature name (for Neo4j lookup),
    return an ImpactReport:

    {
      "unchanged":       [tc_id, ...],
      "update_required": [{"tc_id", "reason", "base_flow_id", "tc": {...}}, ...],
      "regenerate":      [{"tc_id", "reason", "base_flow_id", "tc": {...}}, ...],
      "obsolete":        [{"tc_id", "base_flow_id"}, ...],
      "new_required":    [{"flow_id", "flow_title"}, ...],
      "flow_decisions":  {base_flow_id: [{"tc_id", "action", "reason"}, ...]},
    }
    flow_decisions is populated only for modified and removed flows.
    Used by cmd_delta to build the per-flow approval structure.
    """
    impact = {
        "unchanged": [],
        "update_required": [],
        "regenerate": [],
        "obsolete": [],
        "new_required": [],
        "flow_decisions": {},
    }

    # ── Unchanged flows → all their TCs are fine ──────────────────────────────
    for flow_id in delta_report.get("unchanged", []):
        tcs = graph_service.get_test_cases_for_flow(flow_id)
        impact["unchanged"].extend(tc["tc_id"] for tc in tcs)

    # ── Modified flows → LLM scoring ─────────────────────────────────────────
    for pair in delta_report.get("modified", []):
        old_flow = pair["old"]
        new_flow = pair["new"]
        base_flow_id = old_flow["flow_id"]

        existing_tcs = graph_service.get_test_cases_for_flow(base_flow_id)
        if not existing_tcs:
            impact["new_required"].append({
                "flow_id": new_flow["flow_id"],
                "flow_title": new_flow.get("title", ""),
            })
            impact["flow_decisions"][base_flow_id] = []
            continue

        decisions = _score_tcs(old_flow, new_flow, existing_tcs)
        tc_by_id = {tc["tc_id"]: tc for tc in existing_tcs}
        per_flow = []

        for d in decisions:
            tc_id = d.get("tc_id")
            decision = d.get("decision", "REGENERATE")
            reason = d.get("reason", "")
            tc = tc_by_id.get(tc_id, {"tc_id": tc_id})

            per_flow.append({"tc_id": tc_id, "action": decision, "reason": reason})

            if decision == "UNCHANGED":
                impact["unchanged"].append(tc_id)
            elif decision == "UPDATE_REQUIRED":
                impact["update_required"].append(
                    {"tc_id": tc_id, "reason": reason, "base_flow_id": base_flow_id, "tc": tc}
                )
            else:
                impact["regenerate"].append(
                    {"tc_id": tc_id, "reason": reason, "base_flow_id": base_flow_id, "tc": tc}
                )

        impact["flow_decisions"][base_flow_id] = per_flow

    # ── Removed flows → all TCs are obsolete ─────────────────────────────────
    for flow in delta_report.get("removed", []):
        base_flow_id = flow["flow_id"]
        tcs = graph_service.get_test_cases_for_flow(base_flow_id)
        per_flow = []
        for tc in tcs:
            impact["obsolete"].append({"tc_id": tc["tc_id"], "base_flow_id": base_flow_id})
            per_flow.append({"tc_id": tc["tc_id"], "action": "OBSOLETE", "reason": "flow removed"})
        impact["flow_decisions"][base_flow_id] = per_flow

    # ── Added flows → TCs don't exist yet ────────────────────────────────────
    for flow in delta_report.get("added", []):
        impact["new_required"].append({
            "flow_id": flow["flow_id"],
            "flow_title": flow.get("title", ""),
        })

    return impact


def analyze_api(delta_report: dict, feature_name: str) -> dict:
    """
    Same as analyze() but operates on APITestCase nodes (HAS_API_TC edges).
    Used by the upload-api delta path.
    Returns same ImpactReport shape as analyze().
    """
    impact = {
        "unchanged": [],
        "update_required": [],
        "regenerate": [],
        "obsolete": [],
        "new_required": [],
        "flow_decisions": {},
    }

    for flow_id in delta_report.get("unchanged", []):
        tcs = graph_service.get_api_test_cases_for_flow(flow_id)
        impact["unchanged"].extend(tc["tc_id"] for tc in tcs)

    for pair in delta_report.get("modified", []):
        old_flow = pair["old"]
        new_flow = pair["new"]
        base_flow_id = old_flow["flow_id"]

        existing_tcs = graph_service.get_api_test_cases_for_flow(base_flow_id)
        if not existing_tcs:
            impact["new_required"].append({
                "flow_id": new_flow["flow_id"],
                "flow_title": new_flow.get("title", ""),
            })
            impact["flow_decisions"][base_flow_id] = []
            continue

        decisions = _score_tcs(old_flow, new_flow, existing_tcs)
        tc_by_id = {tc["tc_id"]: tc for tc in existing_tcs}
        per_flow = []

        for d in decisions:
            tc_id = d.get("tc_id")
            decision = d.get("decision", "REGENERATE")
            reason = d.get("reason", "")
            tc = tc_by_id.get(tc_id, {"tc_id": tc_id})

            per_flow.append({"tc_id": tc_id, "action": decision, "reason": reason})

            if decision == "UNCHANGED":
                impact["unchanged"].append(tc_id)
            elif decision == "UPDATE_REQUIRED":
                impact["update_required"].append(
                    {"tc_id": tc_id, "reason": reason, "base_flow_id": base_flow_id, "tc": tc}
                )
            else:
                impact["regenerate"].append(
                    {"tc_id": tc_id, "reason": reason, "base_flow_id": base_flow_id, "tc": tc}
                )

        impact["flow_decisions"][base_flow_id] = per_flow

    for flow in delta_report.get("removed", []):
        base_flow_id = flow["flow_id"]
        tcs = graph_service.get_api_test_cases_for_flow(base_flow_id)
        per_flow = []
        for tc in tcs:
            impact["obsolete"].append({"tc_id": tc["tc_id"], "base_flow_id": base_flow_id})
            per_flow.append({"tc_id": tc["tc_id"], "action": "OBSOLETE", "reason": "flow removed"})
        impact["flow_decisions"][base_flow_id] = per_flow

    for flow in delta_report.get("added", []):
        impact["new_required"].append({
            "flow_id": flow["flow_id"],
            "flow_title": flow.get("title", ""),
        })

    return impact


def _score_tcs(old_flow: dict, new_flow: dict, test_cases: list[dict]) -> list[dict]:
    """Call LLM to score each TC against the flow change. Returns list of decision dicts."""
    tc_summary = [
        {"tc_id": tc["tc_id"], "title": tc.get("title", ""), "type": tc.get("type", "")}
        for tc in test_cases
    ]

    resp = _llm.generate_response_text(
        system_prompt=_SCORE_SYSTEM,
        user_prompt=_SCORE_USER.format(
            old_flow=json.dumps(old_flow, indent=2),
            new_flow=json.dumps(new_flow, indent=2),
            test_cases=json.dumps(tc_summary, indent=2),
        ),
    )

    cleaned = resp.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: mark everything for regeneration
        return [{"tc_id": tc["tc_id"], "decision": "REGENERATE", "reason": "LLM parse error"} for tc in test_cases]
