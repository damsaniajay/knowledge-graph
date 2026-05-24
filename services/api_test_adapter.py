"""
api_test_adapter.py
Generate API-level test cases from story flows + endpoint context from Neo4j.

Flow:
  1. extract_flows_with_apis() — extracts flows with apis_used AND depends_on
     so inter-flow dependencies are captured (e.g. Recharge depends on Login)
  2. generate_api_test_cases() — generates TCs from flow description only
     (endpoint lookup happens at script generation time, not here)

Graph edges added:
  (:Flow)-[:DEPENDS_ON]->(:Flow)  — stored so script generator can traverse
                                     the full dependency chain per flow
"""

import json
import config

from components.common.llm_client import LLMClient

_llm = LLMClient(api_key=config.OPENAI_API_KEY, model=config.LLM_MODEL)

# ── Prompts ───────────────────────────────────────────────────────────────────

_EXTRACT_FLOWS_SYSTEM = "You are a QA architect who extracts functional flows, their API dependencies, and inter-flow dependencies from user stories."

_EXTRACT_FLOWS_USER = """Analyze the following user story and extract every distinct functional flow.
For each flow identify:
  - which APIs it calls
  - which other flows must complete BEFORE this flow can run (depends_on)

Return a JSON array — no other text:
[
  {{
    "flow_id": "f1",
    "title": "Short flow name",
    "description": "One sentence describing what this flow covers",
    "steps": ["Step 1 ...", "Step 2 ..."],
    "rules_conditions": ["Any business rule"],
    "errors_exceptions": ["Any error scenario"],
    "apis_used": ["Login API", "Recharge API"],
    "depends_on": []
  }},
  {{
    "flow_id": "f2",
    "title": "Plan Selection",
    "description": "...",
    "steps": ["..."],
    "rules_conditions": [],
    "errors_exceptions": [],
    "apis_used": ["Plan Catalog API"],
    "depends_on": ["f1"]
  }}
]

Rules:
- flow_id must be sequential: f1, f2, f3 ...
- depends_on must contain flow_ids of flows that must run first
- If a flow has no prerequisites, use an empty list for depends_on
- apis_used should name the APIs implied in the story for this flow

User Story:
{story}
"""

_GENERATE_API_TCS_SYSTEM = "You are a QA architect generating API-level test cases from user story flows."

_GENERATE_API_TCS_USER = """Generate API test cases for the following flow.
Use only the flow description and steps — no endpoint schema is available at this stage.

FLOW:
{flow}

Generate a maximum of 4 test cases: 1 positive, 1 negative, 1 boundary, 1 error.
Only generate fewer if the flow does not warrant a particular type.
For each test case infer the most likely REST endpoint path and method from the flow context.

Return a JSON array — no other text:
[
  {{
    "tc_id": "ATC-{flow_id}-001",
    "title": "Short descriptive title",
    "type": "positive|negative|boundary|error",
    "endpoint_path": "/inferred/path",
    "endpoint_method": "GET|POST|PUT|DELETE",
    "preconditions": "What must be true before this test runs",
    "steps": [
      "Send METHOD /path with body describing the scenario",
      "Verify response status is XXX",
      "Verify response contains expected field"
    ],
    "expected_result": "HTTP XXX with specific response description"
  }}
]
"""


# ── Public API ────────────────────────────────────────────────────────────────

def extract_flows_with_apis(story_text: str) -> list[dict]:
    """
    Extract flows from story text, including apis_used and depends_on fields.
    """
    response_text = _llm.generate_response_text(
        system_prompt=_EXTRACT_FLOWS_SYSTEM,
        user_prompt=_EXTRACT_FLOWS_USER.format(story=story_text)
    )
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def generate_api_test_cases(flows: list[dict]) -> dict[str, list]:
    """
    Generate API test cases from flow descriptions only.
    No endpoint lookup at this stage — endpoint_path/method in each TC
    are inferred by the LLM and used later by the script generator.

    Returns {flow_id: [tc_dict, ...]}
    """
    all_tcs = {}

    for flow in flows:
        flow_slim = {
            "flow_id": flow["flow_id"],
            "title": flow["title"],
            "description": flow.get("description", ""),
            "steps": flow.get("steps", []),
        }
        prompt = _GENERATE_API_TCS_USER.format(
            flow=json.dumps(flow_slim, indent=2),
            flow_id=flow["flow_id"],
        )

        response_text = _llm.generate_response_text(
            system_prompt=_GENERATE_API_TCS_SYSTEM,
            user_prompt=prompt,
        )

        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()

        try:
            tcs = json.loads(cleaned)
        except json.JSONDecodeError as e:
            print(f"    [warn] Failed to parse TCs for flow '{flow['title']}': {e}")
            tcs = []

        for i, tc in enumerate(tcs, start=1):
            if not tc.get("tc_id"):
                tc["tc_id"] = f"ATC-{flow['flow_id']}-{i:03d}"

        all_tcs[flow["flow_id"]] = tcs

    return all_tcs
