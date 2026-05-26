# Test Cases — Schema (manual + LLM, both phased)

**Aligned:** Test cases use **both** channels over time — same approval pattern as LLM flow extraction on stories.

| Phase | Source | Approval |
|-------|--------|----------|
| **v1 (demo)** | **Manual upload** (JSON files) | Optional light validation only |
| **v2 (next)** | **LLM-generated** from story/features | **Human approval** required before Neo4j |

---

## v1 — Manual upload (now)

### Who provides

Team / QA uploads `TC-*.json` after story + features exist in Neo4j.

### Required fields

```json
{
  "tc_id": "TC-login-001",
  "linked_to": "Login",
  "title": "Valid login with correct credentials",
  "type": "positive",
  "test_layer": "api",
  "steps": ["..."],
  "expected_result": "Session token returned"
}
```

### Rules

| Rule | Validation |
|------|------------|
| `linked_to` must resolve | Story id, feature id, or `METHOD:path` in graph — else **reject** (F-N4) |
| `tc_id` unique per base_id | Versioning like other entities |
| Link | `HAS_TEST_CASE` created by mapper on commit |

### API (existing)

- `POST /api/nodes/testcases`
- `POST /api/upload` (testcase json)

### Demo

1. Upload story (with `flows[]`) + features + API  
2. Upload `TC-login-001.json`, `TC-planfetch-001.json`, … (see `sample_data/testcases/`)  
3. Show `HAS_TEST_CASE` edges  
4. Story/API v2 → **impact** flags stale TCs  

---

## v2 — LLM-generated (later)

### Trigger

After story/features are in the graph, optional:

`POST /api/test-proposals/extract?feature_id=Login&story_id=US1`

### LLM input

- UserStory `flows[]` + feature descriptions  
- Feature + API context from modeling layer  
- Test types: positive, negative, boundary  

### Output (proposal)

```json
{
  "proposal_id": "tprop-xyz",
  "linked_to": "Login",
  "status": "pending_approval",
  "test_cases": [
    {
      "tc_id": "TC-login-001",
      "action": "create",
      "title": "Valid login",
      "type": "positive",
      "steps": ["..."],
      "expected_result": "..."
    },
    {
      "tc_id": "TC-login-002",
      "action": "create",
      "title": "Login with wrong password",
      "type": "negative",
      "steps": ["..."],
      "expected_result": "401"
    }
  ]
}
```

### Delta on story/feature v2

| action | Meaning |
|--------|---------|
| `unchanged` | Keep current TC version |
| `create` | New TC |
| `update` | New TC version |
| `deprecate` | Expire TC no longer valid |

Human **approve → commit** — same lifecycle as LLM flow proposals on stories.

### Negative testing (Aravinda requirement)

LLM must propose **negative** cases explicitly:

| ID | Scenario |
|----|----------|
| T-N1 | Wrong password / invalid input |
| T-N2 | TC `linked_to` not in graph — reject |
| T-N3 | Commit without approval — blocked |
| T-N4 | Feature/story v2 changes scope — delta TC proposal only for affected TCs |

---

## Side-by-side

| | Story flows[] | Test cases v1 | Test cases v2 |
|---|-------|---------------|----------------|
| Source | LLM on story | Manual JSON | LLM |
| Approval | Required | Not required | Required |
| Delta on parent change | Yes | N/A (manual re-upload) | Yes |
| API prefix | `/api/flow-proposals` | `/api/nodes/testcases` | `/api/test-proposals` (future) |

---

## What to tell Aravinda

> “Test cases: **manual upload for the demo** once flows are approved and committed. **LLM-generated test cases with human approval and delta updates** are phase 2, same pattern as flows.”
