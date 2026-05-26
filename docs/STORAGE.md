# Where data is stored

The **live graph** is stored in **Neo4j** (`NEO4J_URI` in `.env`). The schema document’s SQL **history** tables are optional and implemented in **PostgreSQL** when `DATABASE_URL` is set — see **`docs/GRAPHITI_AND_TRACKING.md`**.

There is no separate “response schema table” in a relational database — each OpenAPI response becomes an **`APIResponseSchema`** node linked from **`APIEndpoint`**.

## Mental model

| Doc / spreadsheet term | Actual storage |
|------------------------|----------------|
| “User story table” | Neo4j nodes with label **`UserStory`** |
| “Feature table” | Nodes labeled **`Feature`** |
| “API table” | Nodes labeled **`APIEndpoint`** |
| “Response schema table” | Nodes labeled **`APIResponseSchema`** |
| “Test case table” | Nodes labeled **`TestCase`** |
| “Flows” | **Property `flows`** on the `UserStory` node in Neo4j (LLM-derived at upload) — **not** in story JSON files and **not** separate nodes |
| Foreign keys / joins | **Relationships (edges)** between nodes |

## Node properties (examples)

| Label | Stable id property | Versioned id | Other important fields |
|-------|-------------------|--------------|-------------------------|
| UserStory | `base_id` (= `story_id`) | `node_id` e.g. `US1_v1` | `title`, `content`, **`flows`[]** |
| Feature | `base_id` (= `feature_id`) | `node_id` e.g. `Login_v1` | `name`, `apis_used`[] |
| APIEndpoint | `base_id` (= `METHOD:path`) | `node_id` e.g. `POST:/auth/login_v2` | `path`, `method`, `request_schema` |
| APIResponseSchema | `base_id` | `node_id` | `endpoint_id`, `status_code`, `schema` |
| TestCase | `base_id` (= `tc_id`) | `node_id` | `linked_to`, `type`, `steps` |

Every node also has: `version`, `is_current`, `status`, `valid_from`, `valid_to`, plus audit fields (`created_at`, `created_by`, …). See **`docs/VERSIONING.md`** for why this is not redundant with `status` alone.

Legacy property names `valid_at` / `invalid_at` are migrated to `valid_from` / `valid_to` via `setup-schema` or `POST /api/graph/repair-schema`.

## Relationships (edges)

Stored as Neo4j relationships with `type(r)` = edge name, e.g. `HAS_FEATURE`, `USES_API`.

| Edge | Stored as |
|------|-----------|
| UserStory → Feature | `(:UserStory)-[:HAS_FEATURE]->(:Feature)` |
| UserStory → APIEndpoint | `(:UserStory)-[:USES_API]->(:APIEndpoint)` |
| UserStory → TestCase | `(:UserStory)-[:HAS_TEST_CASE]->(:TestCase)` |
| Feature → APIEndpoint | `(:Feature)-[:USES_API {params, coupling_type}]->(:APIEndpoint)` |
| Feature → TestCase | `(:Feature)-[:HAS_TEST_CASE]->(:TestCase)` |
| APIEndpoint → TestCase | `(:APIEndpoint)-[:HAS_TEST_CASE]->(:TestCase)` |
| Feature → Feature (order) | `(:Feature)-[:NEXT_STEP]->(:Feature)` from `UserStory.flows[]` |
| APIEndpoint → APIResponseSchema | `(:APIEndpoint)-[:HAS_RESPONSE_SCHEMA]->(:APIResponseSchema)` |
| TestCase → APIResponseSchema | `(:TestCase)-[:VALIDATES_AGAINST]->(:APIResponseSchema)` (negative tests) |

## Source files vs database

| Location | Role |
|----------|------|
| `sample_data/` | **Demo input files** (JSON/YAML) — not the database |
| Upload / API | Parses files → writes **Neo4j** |
| Neo4j server | **System of record** (`NEO4J_URI` in `.env`) |
| Browser | **View only** — reads via REST `/api/graph` |

Flow proposals (`/api/flow-proposals/*`) are held **in server memory** until committed; then `flows[]` is written on the UserStory node in Neo4j.

## What is *not* persisted

| Item | Where it lives |
|------|----------------|
| `sample_data/*.json` | Git repo only (upload inputs) |
| Browser UI state | Session only (refreshed from API) |
| Flow proposals (pending) | Python process memory until approve/commit |
| PostgreSQL `entity_history`, `upload_events`, `delta_events` | Optional audit trail (`DATABASE_URL`) |
| Flow proposals (pending) | Python process memory until committed |

## Verify your Neo4j database

Connection string: **`NEO4J_URI`** in `.env` (e.g. `neo4j://host:7687`). Open **Neo4j Browser** on that host and run:

## Useful Cypher (Neo4j Browser)

```cypher
// All current nodes
MATCH (n) WHERE n.is_current = true
  AND any(l IN labels(n) WHERE l IN ['UserStory','Feature','APIEndpoint','APIResponseSchema','TestCase'])
RETURN labels(n)[0] AS type, n.base_id, n.node_id, n.version;

// All edge types from US1
MATCH (us:UserStory {base_id:'US1', is_current:true})-[r]->(m)
RETURN type(r) AS edge, labels(m)[0] AS to, m.base_id;
```
