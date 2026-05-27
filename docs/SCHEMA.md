# Knowledge Graph Schema (canonical)

Reference: `KnowledgeGraph_Schema (3).docx`  
Storage details: **`docs/STORAGE.md`**

## Flows (not graph nodes)

**Flows are stored as a list on the UserStory node** (`flows: ["Login", "PlanFetch", …]`), not as separate nodes.

- Order of the journey = `UserStory.flows[]`
- Flow order lives on `UserStory.flows[]` only; structural deps = `DEPENDS_ON` (dependent → prerequisite)
- **Never** in story upload JSON — filled by **LLM** (or heuristic if no API key) on story upload and stored on the Neo4j UserStory node

## Node types

| Node | Role |
|------|------|
| **UserStory** | Business requirement + **`flows[]`** |
| **Feature** | Capability; `apis_used[]` for API linking |
| **APIEndpoint** | One REST operation (`base_id` = `METHOD:path`) |
| **APIResponseSchema** | Response body per HTTP status (from OpenAPI) |
| **TestCase** | `linked_to` → story id, feature id, or `METHOD:path` |

## Required relationships (demo coverage)

```
UserStory  -[HAS_FEATURE]->     Feature
UserStory  -[USES_API]->        APIEndpoint
UserStory  -[HAS_TEST_CASE]->    TestCase

Feature    -[USES_API]->        APIEndpoint
Feature    -[HAS_TEST_CASE]->   TestCase
Feature    -[DEPENDS_ON]->       Feature          (dependent → prerequisite from flows[] order)

APIEndpoint -[HAS_TEST_CASE]->  TestCase
APIEndpoint -[HAS_RESPONSE_SCHEMA]-> APIResponseSchema

TestCase   -[VALIDATES_AGAINST]-> APIResponseSchema   (negative tests, optional)
```

Also supported: `DEPENDS_ON`, `BLOCKS` (story↔story, feature↔feature).

## Versioning

Append-only in Neo4j (`node_id`, `is_current`, `status`, `valid_from`, `valid_to`). Details: **`docs/VERSIONING.md`**.

## Demo upload order

See **`sample_data/README.md`** and **`sample_data/DEMO_COVERAGE.md`**.

## Implementation

| Concern | Module |
|---------|--------|
| Neo4j CRUD + export | `services/graph_service.py` |
| Auto-linking on upload | `services/linking_engine.py` |
| OpenAPI ingest | `services/openapi_ingest.py` |
| Flow derivation | `services/flow_derivation.py` |
| Constraints | `services/schema_service.py` |
