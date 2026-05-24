"""
story_adapter.py
Bridge between plain-text story files and the existing IntelliQ backend services.

  1. extract_flows()     — uses LLMClient from existing backend to parse a story
                           into structured flows (avoids needing a full DOCX pipeline)
  2. generate_test_cases() — uses TestCaseGenerator from existing backend
                             (reuses exact same prompts as production)
"""

import json
import config  # adds BACKEND_PATH to sys.path

from components.common.llm_client import LLMClient
from components.test_case_generator.test_case_generator import TestCaseGenerator

_llm = LLMClient(api_key=config.OPENAI_API_KEY, model=config.LLM_MODEL)

# ── Prompts used only in this prototype ──────────────────────────────────────

_EXTRACT_FLOWS_SYSTEM = "You are a QA architect who extracts functional flows from user stories."

_EXTRACT_FLOWS_USER = """Analyze the following user story and extract every distinct functional flow.

Return a JSON array — no other text:
[
  {{
    "flow_id": "f1",
    "title": "Short flow name",
    "description": "One sentence describing what this flow covers",
    "steps": ["Step 1 ...", "Step 2 ...", "Step 3 ..."],
    "rules_conditions": ["Any business rule or condition"],
    "errors_exceptions": ["Any error scenario"]
  }}
]

Rules:
- flow_id must be sequential: f1, f2, f3 ...
- Each flow is one distinct user journey
- Steps must be specific and testable

User Story:
{story}
"""

# ── Public API ────────────────────────────────────────────────────────────────

def extract_flows(story_text: str) -> list[dict]:
    """
    Call LLMClient to convert plain story text into structured flows JSON.
    Returns a list of flow dicts compatible with TestCaseGenerator.
    """
    response_text = _llm.generate_response_text(
        system_prompt=_EXTRACT_FLOWS_SYSTEM,
        user_prompt=_EXTRACT_FLOWS_USER.format(story=story_text)
    )
    # Strip markdown fences if LLM wraps the JSON
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def generate_test_cases(flows: list[dict], story_text: str) -> dict[str, list]:
    """
    Use the existing TestCaseGenerator (same prompts as production) to generate
    functional test cases for each flow.

    context_knowledge is passed as "" — no RAG in this prototype.
    business_context is built from the story text.

    Returns: { flow_id: [tc_dict, ...], ... }
    """
    business_context = {
        "business_objective": story_text[:800],
        "scope": {"in_scope": [], "out_of_scope": []},
        "actors_permissions": {},
        "assumptions": [],
        "prerequisites": [],
        "dependencies": [],
        "constraints": [],
        "non_functional_requirements": []
    }

    all_test_cases = {}

    for flow in flows:
        generator = TestCaseGenerator(
            flow=flow,
            business_context=business_context,
            context_knowledge="",         # no RAG in prototype
            platform="web",
            api_key=config.OPENAI_API_KEY,
            model_name=config.LLM_MODEL
        )

        flow_tcs = []
        typed_tcs: list[tuple[str, dict]] = []
        for tc_type in ["positive", "negative", "boundary", "error"]:
            try:
                tcs = generator.generate_test_cases_for_type(
                    test_case_type=tc_type,
                    existing_descriptions=[t.get("test_case_description", "") for t in [r for _, r in typed_tcs]]
                )
                typed_tcs.extend((tc_type, tc) for tc in tcs)
            except Exception as e:
                print(f"    [warn] {tc_type} generation failed for '{flow['title']}': {e}")

        # Normalise to a flat dict format for Neo4j storage
        all_test_cases[flow["flow_id"]] = _normalise(flow["flow_id"], typed_tcs)

    return all_test_cases


# ── Internal helpers ──────────────────────────────────────────────────────────

def _normalise(flow_id: str, typed_tcs: list[tuple[str, dict]]) -> list[dict]:
    """
    TestCaseGenerator returns its own dict shape. Flatten it to a simple
    format for Neo4j and the delta report.
    typed_tcs is a list of (tc_type_str, raw_tc_dict) pairs so the type
    is always known even if the generator doesn't echo it back.
    """
    normalised = []
    for i, (tc_type, tc) in enumerate(typed_tcs, start=1):
        steps_raw = tc.get("test_steps") or tc.get("steps") or []
        if steps_raw and isinstance(steps_raw[0], dict):
            steps = [s.get("step", "") for s in steps_raw]
        else:
            steps = steps_raw

        normalised.append({
            "tc_id": tc.get("test_case_id") or f"TC-{flow_id}-{i:03d}",
            "title": tc.get("test_case_description") or tc.get("title", f"TC {i}"),
            "type": tc.get("test_case_type") or tc.get("type") or tc_type,
            "preconditions": str(tc.get("pre_requisites") or tc.get("preconditions") or ""),
            "steps": steps,
            "expected_result": (steps[-1] if steps else "")
        })
    return normalised
