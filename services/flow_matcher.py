"""
flow_matcher.py
Three-phase FlowMatcher that compares two versions of flows JSON.

Phase 1 — Exact title match   (O(n) dict lookup)
Phase 2 — Hash diff + LLM confirm  (hash mismatch triggers a YES/NO LLM call to
                                    filter out false positives from non-deterministic
                                    re-extraction of unchanged sections)
Phase 3 — LLM semantic diff   (detect renames / near-duplicate flows in leftovers)

Returns a DeltaReport dict:
{
  "unchanged":  [flow_id, ...],
  "modified":   [{"old": flow_v1, "new": flow_v2}, ...],
  "added":      [flow_v2_dict, ...],
  "removed":    [flow_v1_dict, ...],
}
"""

import hashlib
import json
import config

from components.common.llm_client import LLMClient

_llm = LLMClient(api_key=config.OPENAI_API_KEY, model=config.LLM_MODEL)

# ── Prompts ───────────────────────────────────────────────────────────────────

_CONFIRM_CHANGE_SYSTEM = (
    "You are a QA architect. Answer with a single word: YES or NO. No explanation."
)

_CONFIRM_CHANGE_USER = """Are these two flow descriptions functionally different?
Focus only on actual changes to steps, rules, or behavior.
Ignore minor wording differences that mean the same thing.

FLOW V1:
{f1}

FLOW V2:
{f2}

Answer YES if there is a real functional difference, NO if they describe the same behavior.
"""

_SEMANTIC_DIFF_SYSTEM = (
    "You are a QA architect comparing two sets of functional flows. "
    "Identify flows that are semantically the same (just renamed or slightly reworded)."
)

_SEMANTIC_DIFF_USER = """You have two lists of flows that could not be matched by title.

Unmatched flows from VERSION 1:
{v1_flows}

Unmatched flows from VERSION 2:
{v2_flows}

For each flow in V2 that is semantically equivalent to a flow in V1 (same user journey, even if renamed),
return a JSON array of match pairs. If there is no equivalent, do not include it.

Return ONLY a JSON array — no other text:
[
  {{"v1_flow_id": "f1", "v2_flow_id": "f2", "confidence": 0.9}},
  ...
]

If there are no matches at all, return an empty array: []
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _flow_hash(flow: dict) -> str:
    """Stable hash over the semantically meaningful fields of a flow."""
    payload = {
        "title": flow.get("title", ""),
        "description": flow.get("description", ""),
        "steps": flow.get("steps", []),
        "rules_conditions": flow.get("rules_conditions", []),
        "errors_exceptions": flow.get("errors_exceptions", []),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()


def _by_title(flows: list[dict]) -> dict[str, dict]:
    return {f["title"].strip().lower(): f for f in flows}


def _is_functionally_changed(f1: dict, f2: dict) -> bool:
    """
    LLM confirmation step after a hash mismatch.
    Returns True only if the LLM confirms a real functional difference.
    Defaults to True (treat as changed) if the LLM response is unparseable.
    """
    resp = _llm.generate_response_text(
        system_prompt=_CONFIRM_CHANGE_SYSTEM,
        user_prompt=_CONFIRM_CHANGE_USER.format(
            f1=json.dumps(f1, indent=2),
            f2=json.dumps(f2, indent=2),
        ),
    )
    answer = resp.strip().upper()
    print(f"      [confirm] '{f1['title']}' → {answer}")
    return not answer.startswith("NO")


def _semantic_match(
    unmatched_v1: list[dict], unmatched_v2: list[dict]
) -> list[dict]:
    """
    Ask LLM to pair semantically equivalent flows across the two unmatched sets.
    Returns list of {"v1_flow_id", "v2_flow_id", "confidence"} dicts.
    """
    if not unmatched_v1 or not unmatched_v2:
        return []

    resp = _llm.generate_response_text(
        system_prompt=_SEMANTIC_DIFF_SYSTEM,
        user_prompt=_SEMANTIC_DIFF_USER.format(
            v1_flows=json.dumps(unmatched_v1, indent=2),
            v2_flows=json.dumps(unmatched_v2, indent=2),
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
        return []


def _classify_matched_pair(f1: dict, f2: dict, report: dict) -> None:
    """
    Phase 2 + LLM confirmation for a title-matched pair.
    Writes to report["unchanged"] or report["modified"].
    """
    if _flow_hash(f1) == _flow_hash(f2):
        # Exact hash match — definitely unchanged, no LLM needed
        report["unchanged"].append(f1["flow_id"])
    else:
        # Hash differs — could be real change or LLM re-extraction noise
        if _is_functionally_changed(f1, f2):
            report["modified"].append({"old": f1, "new": f2})
        else:
            report["unchanged"].append(f1["flow_id"])


# ── Public API ────────────────────────────────────────────────────────────────

def compare_flows(flows_v1: list[dict], flows_v2: list[dict]) -> dict:
    """
    Compare two flow lists and return a structured DeltaReport.

    Args:
        flows_v1: baseline flows (from Neo4j snapshot or first parse)
        flows_v2: new flows (from latest story parse)

    Returns:
        {
          "unchanged": [flow_id, ...],
          "modified":  [{"old": {...}, "new": {...}}, ...],
          "added":     [{flow_v2_dict}, ...],
          "removed":   [{flow_v1_dict}, ...],
        }
    """
    report = {"unchanged": [], "modified": [], "added": [], "removed": []}

    # ── Phase 1: exact title match ────────────────────────────────────────────
    v1_by_title = _by_title(flows_v1)
    v2_by_title = _by_title(flows_v2)

    matched_v1_ids: set[str] = set()
    matched_v2_ids: set[str] = set()

    for title_lower, f2 in v2_by_title.items():
        if title_lower in v1_by_title:
            f1 = v1_by_title[title_lower]
            matched_v1_ids.add(f1["flow_id"])
            matched_v2_ids.add(f2["flow_id"])
            # Phase 2 + LLM confirmation
            _classify_matched_pair(f1, f2, report)

    unmatched_v1 = [f for f in flows_v1 if f["flow_id"] not in matched_v1_ids]
    unmatched_v2 = [f for f in flows_v2 if f["flow_id"] not in matched_v2_ids]

    # ── Phase 3: LLM semantic match on leftovers ──────────────────────────────
    if unmatched_v1 and unmatched_v2:
        pairs = _semantic_match(unmatched_v1, unmatched_v2)

        paired_v1_ids: set[str] = set()
        paired_v2_ids: set[str] = set()

        v1_by_id = {f["flow_id"]: f for f in unmatched_v1}
        v2_by_id = {f["flow_id"]: f for f in unmatched_v2}

        for pair in pairs:
            v1_id = pair.get("v1_flow_id")
            v2_id = pair.get("v2_flow_id")
            if v1_id in v1_by_id and v2_id in v2_by_id:
                f1, f2 = v1_by_id[v1_id], v2_by_id[v2_id]
                paired_v1_ids.add(v1_id)
                paired_v2_ids.add(v2_id)
                _classify_matched_pair(f1, f2, report)

        for f in unmatched_v1:
            if f["flow_id"] not in paired_v1_ids:
                report["removed"].append(f)
        for f in unmatched_v2:
            if f["flow_id"] not in paired_v2_ids:
                report["added"].append(f)
    else:
        report["removed"].extend(unmatched_v1)
        report["added"].extend(unmatched_v2)

    return report
