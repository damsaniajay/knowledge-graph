"""
delta_executor.py
Executes per-flow approval decisions: updates / regenerates / creates TCs.

Called from main.py:cmd_approve for each flow the user individually approves:
  modified + UNCHANGED       → TC content copied as-is to the new version
  modified + UPDATE_REQUIRED → LLM patches the TC to match the new flow
  modified + REGENERATE      → LLM fully regenerates the TC from scratch
  modified + OBSOLETE        → TC is soft-deleted (status=obsolete)
  added                      → fresh TC generation for the new flow
  removed                    → all TCs marked obsolete
"""

import json
import config
from components.common.llm_client import LLMClient
from services import graph_service

_llm = LLMClient(api_key=config.OPENAI_API_KEY, model=config.LLM_MODEL)

_UPDATE_SYSTEM = "You are a QA engineer updating a test case to reflect changes in a user story flow."

_UPDATE_USER = """Update this test case so it is accurate for the changed flow.
Keep the same test type and general intent; fix any steps or expected results that no longer apply.

EXISTING TEST CASE:
{tc}

OLD FLOW:
{old_flow}

NEW FLOW:
{new_flow}

Return a JSON object — no other text:
{{
  "title": "Updated title",
  "preconditions": "...",
  "steps": ["Step 1 ...", "Step 2 ..."],
  "expected_result": "HTTP XXX with ..."
}}"""

_REGEN_SYSTEM = "You are a QA architect generating a single {tc_type} test case for an updated user story flow."

_REGEN_USER = """Regenerate this {tc_type} test case for the updated flow below.

UPDATED FLOW:
{flow}

Return a JSON object — no other text:
{{
  "title": "Short descriptive title",
  "preconditions": "What must be true before this test runs",
  "steps": ["Step 1 ...", "Step 2 ..."],
  "expected_result": "HTTP XXX with specific response description"
}}"""

_NEW_TCS_SYSTEM = "You are a QA architect generating test cases for a newly added user story flow."

_NEW_TCS_USER = """Generate test cases for this new flow.
Produce a maximum of 4: 1 positive, 1 negative, 1 boundary, 1 error.
Only include types that genuinely apply to this flow.

FLOW:
{flow}

Return a JSON array — no other text:
[
  {{
    "tc_id": "TC-{flow_id}-001",
    "title": "Short descriptive title",
    "type": "positive|negative|boundary|error",
    "preconditions": "...",
    "steps": ["Step 1 ...", "Step 2 ..."],
    "expected_result": "HTTP XXX with ..."
  }}
]"""


def execute_flow_approval(
    feature_name: str,
    flow_decision: dict,
    pending_version: int,
    approver: str,
) -> list[dict]:
    """
    Execute the approval decision for a single flow.

    flow_decision keys:
      action        — "modified" | "added" | "removed"
      base_flow_id  — unversioned flow ID (e.g. "f2")
      old_title     — title before the change (modified/removed)
      new_title     — title after the change  (modified/added)
      tc_impacts    — list of {tc_id, action, reason} for modified/removed

    Returns list of new TC dicts that were saved to Neo4j.
    """
    action       = flow_decision["action"]
    base_flow_id = flow_decision["base_flow_id"]
    old_version  = pending_version - 1
    versioned_pending = f"{base_flow_id}_v{pending_version}"
    versioned_old     = f"{base_flow_id}_v{old_version}"
    saved = []

    # ── Removed flow ──────────────────────────────────────────────────────────
    if action == "removed":
        old_tcs = graph_service.get_test_cases_for_flow(versioned_old)
        for tc in old_tcs:
            graph_service.expire_test_case(tc["tc_id"])
            print(f"      OBSOLETE   {tc['tc_id']}")
        return []

    # ── Added flow ────────────────────────────────────────────────────────────
    if action == "added":
        new_flow = graph_service.get_flow_node(versioned_pending)
        if not new_flow:
            print(f"      [!] Pending flow {versioned_pending} not found in Neo4j")
            return []
        tcs = _generate_new_tcs(new_flow, base_flow_id)
        for i, tc in enumerate(tcs, 1):
            if not tc.get("tc_id"):
                tc["tc_id"] = f"TC-{base_flow_id}-{i:03d}"
            graph_service.save_test_case(
                base_flow_id, tc,
                version=pending_version, status="approved", created_by=approver,
            )
            saved.append(tc)
            print(f"      NEW        {tc['tc_id']} [{tc.get('type', '?')}]  {tc.get('title', '')[:55]}")
        return saved

    # ── Modified flow ─────────────────────────────────────────────────────────
    new_flow     = graph_service.get_flow_node(versioned_pending)
    old_flow_node = graph_service.get_flow_node(versioned_old)
    if not new_flow:
        print(f"      [!] Pending flow {versioned_pending} not found in Neo4j")
        return []

    old_tcs_list = graph_service.get_test_cases_for_flow(versioned_old)
    old_tc_by_id = {tc["tc_id"]: tc for tc in old_tcs_list}

    for tc_impact in flow_decision.get("tc_impacts", []):
        tc_action = tc_impact["action"]
        tc_id     = tc_impact["tc_id"]
        old_tc    = old_tc_by_id.get(tc_id)
        base_tc_id = tc_id.rsplit("_v", 1)[0] if "_v" in tc_id else tc_id

        if tc_action == "UNCHANGED":
            if not old_tc:
                print(f"      [!] TC {tc_id} not found, skipping")
                continue
            new_tc = {k: v for k, v in old_tc.items() if k != "tc_id"}
            new_tc["tc_id"] = base_tc_id
            graph_service.save_test_case(
                base_flow_id, new_tc,
                version=pending_version, status="approved", created_by=approver,
            )
            saved.append(new_tc)
            print(f"      UNCHANGED  {tc_id} → v{pending_version}")

        elif tc_action == "OBSOLETE":
            if old_tc:
                graph_service.expire_test_case(tc_id)
                print(f"      OBSOLETE   {tc_id}")

        elif tc_action in ("UPDATE_REQUIRED", "REGENERATE"):
            if not old_tc:
                print(f"      [!] TC {tc_id} not found in Neo4j, skipping")
                continue

            if tc_action == "UPDATE_REQUIRED":
                updated = _update_tc(old_tc, old_flow_node or {}, new_flow)
                label = "UPDATED"
            else:
                updated = _regenerate_tc(old_tc, new_flow)
                label = "REGENERATED"

            if updated:
                updated["tc_id"]  = base_tc_id
                updated["type"]   = updated.get("type") or old_tc.get("type", "positive")
                graph_service.save_test_case(
                    base_flow_id, updated,
                    version=pending_version, status="approved", created_by=approver,
                )
                graph_service.expire_test_case(tc_id)
                saved.append(updated)
                print(f"      {label:<11} {tc_id} → {base_tc_id}_v{pending_version}")
            else:
                print(f"      [!] LLM failed for {tc_id}, keeping old TC unchanged")

    return saved


def execute_api_flow_approval(
    feature_name: str,
    flow_decision: dict,
    pending_version: int,
    approver: str,
) -> dict:
    """
    Execute the approval decision for a single flow on the API TC path.
    Handles APITestCase nodes (not TestCase nodes).

    Returns {"saved_tcs": [...], "regen_tc_ids": [...]}
    regen_tc_ids = TC IDs that need script regeneration after this approval.
    """
    from services import graph_service as _gs

    action       = flow_decision["action"]
    base_flow_id = flow_decision["base_flow_id"]
    old_version  = pending_version - 1
    versioned_old = f"{base_flow_id}_v{old_version}"

    saved_tcs   = []
    regen_tc_ids = []  # versioned TC IDs whose scripts need regeneration

    # ── Removed flow ──────────────────────────────────────────────────────────
    if action == "removed":
        old_tcs = _gs.get_api_test_cases_for_flow(versioned_old)
        for tc in old_tcs:
            # Expire scripts first
            scripts = _gs.get_scripts_for_tc(tc["tc_id"])
            for s in scripts:
                _gs.expire_api_script(s["script_id"])
            _gs.expire_api_test_case(tc["tc_id"])
            print(f"      OBSOLETE   {tc['tc_id']}  (script expired)")
        return {"saved_tcs": [], "regen_tc_ids": []}

    # ── Added flow ────────────────────────────────────────────────────────────
    if action == "added":
        versioned_pending = f"{base_flow_id}_v{pending_version}"
        new_flow = _gs.get_flow_node(versioned_pending)
        if not new_flow:
            print(f"      [!] Pending flow {versioned_pending} not found in Neo4j")
            return {"saved_tcs": [], "regen_tc_ids": []}
        tcs = _generate_new_tcs(new_flow, base_flow_id)
        for i, tc in enumerate(tcs, 1):
            if not tc.get("tc_id"):
                tc["tc_id"] = f"TC-{base_flow_id}-{i:03d}"
            _gs.save_api_test_case(
                versioned_pending, tc,
                version=pending_version, status="active", created_by=approver,
            )
            versioned_tc_id = f"{tc['tc_id']}_v{pending_version}"
            saved_tcs.append({**tc, "tc_id": versioned_tc_id})
            regen_tc_ids.append(versioned_tc_id)
            print(f"      NEW        {versioned_tc_id} [{tc.get('type', '?')}]  {tc.get('title', '')[:55]}")
        return {"saved_tcs": saved_tcs, "regen_tc_ids": regen_tc_ids}

    # ── Modified flow ─────────────────────────────────────────────────────────
    versioned_pending = f"{base_flow_id}_v{pending_version}"
    new_flow      = _gs.get_flow_node(versioned_pending)
    old_flow_node = _gs.get_flow_node(versioned_old)
    if not new_flow:
        print(f"      [!] Pending flow {versioned_pending} not found in Neo4j")
        return {"saved_tcs": [], "regen_tc_ids": []}

    old_tcs_list = _gs.get_api_test_cases_for_flow(versioned_old)
    old_tc_by_id = {tc["tc_id"]: tc for tc in old_tcs_list}

    for tc_impact in flow_decision.get("tc_impacts", []):
        tc_action = tc_impact["action"]
        tc_id     = tc_impact["tc_id"]
        old_tc    = old_tc_by_id.get(tc_id)
        base_tc_id = tc_id.rsplit("_v", 1)[0] if "_v" in tc_id else tc_id
        new_versioned_tc_id = f"{base_tc_id}_v{pending_version}"

        if tc_action == "UNCHANGED":
            if not old_tc:
                print(f"      [!] TC {tc_id} not found, skipping")
                continue
            new_tc = {k: v for k, v in old_tc.items()
                      if k not in ("tc_id", "version", "status", "is_current")}
            new_tc["tc_id"] = base_tc_id
            _gs.save_api_test_case(
                versioned_pending, new_tc,
                version=pending_version, status="active", created_by=approver,
            )
            # Move scripts from old TC to new TC (no LLM needed)
            _gs.reassign_api_scripts(tc_id, new_versioned_tc_id)
            _gs.expire_api_test_case(tc_id)
            saved_tcs.append(new_tc)
            print(f"      UNCHANGED  {tc_id} → {new_versioned_tc_id}  (script kept)")

        elif tc_action == "OBSOLETE":
            if old_tc:
                scripts = _gs.get_scripts_for_tc(tc_id)
                for s in scripts:
                    _gs.expire_api_script(s["script_id"])
                _gs.expire_api_test_case(tc_id)
                print(f"      OBSOLETE   {tc_id}  (script expired)")

        elif tc_action in ("UPDATE_REQUIRED", "REGENERATE"):
            if not old_tc:
                print(f"      [!] TC {tc_id} not found, skipping")
                continue

            if tc_action == "UPDATE_REQUIRED":
                updated = _update_tc(old_tc, old_flow_node or {}, new_flow)
                label = "UPDATED"
            else:
                updated = _regenerate_tc(old_tc, new_flow)
                label = "REGENERATED"

            if updated:
                updated["tc_id"] = base_tc_id
                updated["type"]  = updated.get("type") or old_tc.get("type", "positive")
                _gs.save_api_test_case(
                    versioned_pending, updated,
                    version=pending_version, status="active", created_by=approver,
                )
                # Expire old script — new one will be generated after approval loop
                old_scripts = _gs.get_scripts_for_tc(tc_id)
                for s in old_scripts:
                    _gs.expire_api_script(s["script_id"])
                _gs.expire_api_test_case(tc_id)
                saved_tcs.append({**updated, "tc_id": new_versioned_tc_id})
                regen_tc_ids.append(new_versioned_tc_id)
                print(f"      {label:<11} {tc_id} → {new_versioned_tc_id}  (script queued)")
            else:
                print(f"      [!] LLM failed for {tc_id}, keeping old TC + script")

    return {"saved_tcs": saved_tcs, "regen_tc_ids": regen_tc_ids}


# ── LLM helpers ───────────────────────────────────────────────────────────────

def _update_tc(old_tc: dict, old_flow: dict, new_flow: dict) -> dict | None:
    response = _llm.generate_response_text(
        system_prompt=_UPDATE_SYSTEM,
        user_prompt=_UPDATE_USER.format(
            tc=json.dumps(old_tc, indent=2),
            old_flow=json.dumps(old_flow, indent=2),
            new_flow=json.dumps(new_flow, indent=2),
        ),
    )
    return _parse_obj(response)


def _regenerate_tc(old_tc: dict, new_flow: dict) -> dict | None:
    tc_type = old_tc.get("type", "positive")
    response = _llm.generate_response_text(
        system_prompt=_REGEN_SYSTEM.format(tc_type=tc_type),
        user_prompt=_REGEN_USER.format(
            tc_type=tc_type,
            flow=json.dumps(new_flow, indent=2),
        ),
    )
    return _parse_obj(response)


def _generate_new_tcs(new_flow: dict, base_flow_id: str) -> list[dict]:
    response = _llm.generate_response_text(
        system_prompt=_NEW_TCS_SYSTEM,
        user_prompt=_NEW_TCS_USER.format(
            flow=json.dumps(new_flow, indent=2),
            flow_id=base_flow_id,
        ),
    )
    cleaned = _strip_fences(response)
    try:
        return json.loads(cleaned)
    except Exception:
        return []


def _parse_obj(response: str) -> dict | None:
    cleaned = _strip_fences(response)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
