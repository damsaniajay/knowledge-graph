"""
api_script_service.py
Stage 2 — Script generation.

For each APITestCase stored in Neo4j:
  1. Look up the matching APIEndpoint using endpoint_path + endpoint_method
  2. Call LLMClient with TC + full endpoint schema as context
  3. Generate a Python pytest + requests script
  4. Save APITestScript node to Neo4j
  5. Create COVERS_ENDPOINT edge  ← the key graph link for delta impact

This COVERS_ENDPOINT edge is what enables Scenario C:
  when a new API spec is uploaded, we traverse this edge backwards to find
  all scripts (and their parent TCs) that need to be updated.
"""

import json
import config

from components.common.llm_client import LLMClient
from services import graph_service

_llm = LLMClient(api_key=config.OPENAI_API_KEY, model=config.LLM_MODEL)

_SCRIPT_SYSTEM = "You are a QA engineer writing Python pytest test scripts using the requests library."

_MATCH_AND_SCRIPT_USER = """You have a test case, a list of all available endpoints, and the prerequisite
endpoints that must be called BEFORE this test case (from dependent flows).

STEP 1: Find the primary endpoint from the available list that best matches this test case.
STEP 2: Write a pytest function that:
  - First calls all prerequisite endpoints (setup steps) in order
  - Then calls the primary endpoint for the actual test assertion

TEST CASE:
{tc}

PRIMARY ENDPOINT OPTIONS (pick the best match for this TC):
{endpoints}

PREREQUISITE ENDPOINTS (must be called first, in this order):
{prereq_endpoints}

BASE_URL = "https://api.airtel.com"

Rules:
- Write a single pytest function named test_<snake_case_title>
- Call prerequisite endpoints first to set up state (e.g. login to get token)
- Use the token/response from prerequisite calls in subsequent requests
- Assert only on the PRIMARY endpoint response
- Use requests library, include required fields from schemas
- Add a short docstring with the TC title

Return a JSON object — no other text:
{{
  "matched_endpoint_id": "POST:/auth/login",
  "code": "def test_login_valid_credentials():\\n    ..."
}}
"""


def generate_scripts_for_tc_ids(tc_ids: list[str]) -> list[dict]:
    """
    Generate scripts for a specific list of versioned APITestCase IDs.
    Used after approve-api to generate scripts only for changed/new TCs.
    Returns list of saved script dicts.
    """
    from services import graph_service as _gs

    all_endpoints = _gs.get_all_endpoints()
    if not all_endpoints:
        print("  [!] No endpoints in Neo4j — cannot generate scripts.")
        return []

    endpoint_summaries = [
        {
            "endpoint_id":     ep["endpoint_id"],
            "path":            ep["path"],
            "method":          ep["method"],
            "summary":         ep.get("summary", ""),
            "request_schema":  json.loads(ep.get("request_schema", "{}")),
            "response_schema": json.loads(ep.get("response_schema", "{}")),
        }
        for ep in all_endpoints
    ]
    valid_endpoint_ids = {ep["endpoint_id"] for ep in endpoint_summaries}

    saved = []
    for tc_id in tc_ids:
        # Look up TC from Neo4j
        with _gs._get_driver().session() as session:
            rec = session.run(
                """
                MATCH (tc:APITestCase {tc_id: $tc_id})
                RETURN tc.tc_id AS tc_id, tc.title AS title, tc.type AS type,
                       tc.preconditions AS preconditions, tc.steps AS steps,
                       tc.expected_result AS expected_result,
                       tc.endpoint_path AS endpoint_path,
                       tc.endpoint_method AS endpoint_method
                """,
                tc_id=tc_id,
            ).single()
        if not rec:
            print(f"  [!] TC {tc_id} not found in Neo4j")
            continue
        tc = dict(rec)
        if isinstance(tc.get("steps"), str):
            import json as _json
            tc["steps"] = _json.loads(tc["steps"])

        result = _match_and_generate(tc, endpoint_summaries, [])
        if not result:
            print(f"  [!] TC {tc_id}: LLM failed to generate script")
            continue

        endpoint_id = result["matched_endpoint_id"]
        if endpoint_id not in valid_endpoint_ids:
            print(f"  [warn] Hallucinated endpoint '{endpoint_id}' for {tc_id} — skipping")
            continue

        script = {
            "script_id":   f"SCRIPT-{tc_id}",
            "tc_id":       tc_id,
            "language":    "python",
            "code":        result["code"],
            "endpoint_id": endpoint_id,
        }
        _gs.save_api_script(tc_id=tc_id, script=script, endpoint_id=endpoint_id)
        saved.append(script)
        print(f"    ✓ {script['script_id']}  →  COVERS_ENDPOINT  →  {endpoint_id}")

    return saved


def regenerate_scripts_for_tcs(tc_ids: list[str]) -> list[dict]:
    """
    Re-generate scripts for a specific list of APITestCase IDs.
    Called after an endpoint schema change is approved.
    Deletes the old script and replaces it with a freshly generated one.
    Returns list of regenerated script dicts.
    """
    from services import graph_service as _gs

    all_endpoints = _gs.get_all_endpoints()
    if not all_endpoints:
        print("  [!] No endpoints in Neo4j — cannot regenerate scripts.")
        return []

    endpoint_summaries = [
        {
            "endpoint_id": ep["endpoint_id"],
            "path":        ep["path"],
            "method":      ep["method"],
            "summary":     ep.get("summary", ""),
            "request_schema":  json.loads(ep.get("request_schema", "{}")),
            "response_schema": json.loads(ep.get("response_schema", "{}")),
        }
        for ep in all_endpoints
    ]

    regenerated = []
    for tc_id in tc_ids:
        old_scripts = _gs.get_scripts_for_tc(tc_id)
        tc = None
        if old_scripts:
            tc = _gs.get_script_tc(old_scripts[0]["script_id"])
        if not tc:
            print(f"  [!] TC {tc_id}: could not find linked APITestCase")
            continue

        result = _match_and_generate(tc, endpoint_summaries, [])
        if not result:
            print(f"  [!] TC {tc_id}: LLM failed to generate script")
            continue

        for s in old_scripts:
            _gs.delete_api_script(s["script_id"])

        script = {
            "script_id":   f"SCRIPT-{tc_id}",
            "tc_id":       tc_id,
            "language":    "python",
            "code":        result["code"],
            "endpoint_id": result["matched_endpoint_id"],
        }
        _gs.save_api_script(
            tc_id=tc_id,
            script=script,
            endpoint_id=result["matched_endpoint_id"],
        )
        regenerated.append(script)
        print(f"  ✓ REGENERATED {script['script_id']}  →  {result['matched_endpoint_id']}")

    return regenerated


def generate_scripts_for_feature(feature_name: str) -> list[dict]:
    """
    Generate scripts for all API test cases of a feature.
    Loads all stored endpoints once, then for each TC asks the LLM to
    pick the best matching endpoint and generate the script in one call.
    Returns list of script dicts that were saved.
    """
    flows = graph_service.get_all_flows_for_feature(feature_name)
    if not flows:
        print(f"  [!] No flows found for '{feature_name}'")
        return []

    # Load all endpoints once — passed to LLM for semantic matching
    all_endpoints = graph_service.get_all_endpoints()
    if not all_endpoints:
        print("  [!] No endpoints found in Neo4j. Run ingest-api first.")
        return []

    endpoint_summaries = [
        {
            "endpoint_id": ep["endpoint_id"],
            "path": ep["path"],
            "method": ep["method"],
            "summary": ep.get("summary", ""),
            "request_schema": json.loads(ep.get("request_schema", "{}")),
            "response_schema": json.loads(ep.get("response_schema", "{}")),
        }
        for ep in all_endpoints
    ]

    saved_scripts = []

    for flow in flows:
        tcs = graph_service.get_api_test_cases_for_flow(flow["flow_id"])
        if not tcs:
            continue

        print(f"  Flow {flow['flow_id']} — {flow['title']} ({len(tcs)} TC(s))")

        # Traverse DEPENDS_ON chain to get prerequisite endpoints
        dep_chain = graph_service.get_dependency_chain(flow["flow_id"])
        prereq_endpoints = []
        if dep_chain:
            dep_names = " → ".join(d["title"] for d in dep_chain)
            print(f"    [deps] {dep_names}")
            # Collect endpoints from all dependency flows (already stored scripts)
            chain_endpoints = graph_service.get_all_endpoints_for_flow_chain(flow["flow_id"])
            # Only keep endpoints from dependency flows, not this flow itself
            prereq_endpoints = [
                e for e in chain_endpoints
                if e.get("from_flow") != flow["flow_id"]
            ]

        # Build a fast lookup set of valid endpoint IDs
        valid_endpoint_ids = {ep["endpoint_id"] for ep in endpoint_summaries}

        for tc in tcs:
            result = _match_and_generate(tc, endpoint_summaries, prereq_endpoints)
            if not result:
                print(f"    [warn] Could not match or generate script for '{tc['tc_id']}'")
                continue

            endpoint_id = result["matched_endpoint_id"]

            # Guard: LLM sometimes returns hallucinated endpoint IDs not in the spec
            if endpoint_id not in valid_endpoint_ids:
                # Find closest real endpoint by path fragment for a helpful message
                hint = next(
                    (eid for eid in valid_endpoint_ids
                     if eid.split(":", 1)[-1] in endpoint_id or endpoint_id.split(":", 1)[-1] in eid),
                    None
                )
                hint_msg = f"  (did you mean {hint}?)" if hint else ""
                print(f"    [warn] LLM matched non-existent endpoint '{endpoint_id}'{hint_msg} — skipping {tc['tc_id']}")
                continue

            script = {
                "script_id": f"SCRIPT-{tc['tc_id']}",
                "tc_id": tc["tc_id"],
                "language": "python",
                "code": result["code"],
                "endpoint_id": endpoint_id,
            }

            graph_service.save_api_script(
                tc_id=tc["tc_id"],
                script=script,
                endpoint_id=endpoint_id,
            )

            prereq_tag = f" (prereqs: {len(prereq_endpoints)})" if prereq_endpoints else ""
            print(f"    ✓ {script['script_id']}  →  COVERS_ENDPOINT  →  {endpoint_id}{prereq_tag}")
            saved_scripts.append(script)

    return saved_scripts


def _match_and_generate(tc: dict, endpoint_summaries: list[dict], prereq_endpoints: list[dict] = None) -> dict | None:
    """
    Single LLM call: pick the best matching endpoint from the stored list
    and generate the pytest script for this TC.
    Returns {"matched_endpoint_id": str, "code": str} or None on failure.
    """
    tc_summary = {
        "tc_id": tc["tc_id"],
        "title": tc["title"],
        "type": tc["type"],
        "preconditions": tc.get("preconditions", ""),
        "steps": tc.get("steps", []),
        "expected_result": tc.get("expected_result", ""),
    }

    prereq_summary = [
        {
            "endpoint_id": e["endpoint_id"],
            "path": e["path"],
            "method": e["method"],
            "summary": e.get("summary", ""),
            "from_flow": e.get("from_flow_title", ""),
            "request_schema": json.loads(e.get("request_schema", "{}")),
        }
        for e in (prereq_endpoints or [])
    ]

    response = _llm.generate_response_text(
        system_prompt=_SCRIPT_SYSTEM,
        user_prompt=_MATCH_AND_SCRIPT_USER.format(
            tc=json.dumps(tc_summary, indent=2),
            endpoints=json.dumps(endpoint_summaries, indent=2),
            prereq_endpoints=json.dumps(prereq_summary, indent=2),
        ),
    )

    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None
