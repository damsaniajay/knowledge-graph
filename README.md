# Knowledge Graph -- Relationship Intelligence for Test Engineering

## Table of Contents

1. [The Vision](#1-the-vision)
2. [What We Built](#2-what-we-built)
3. [Architecture](#3-architecture)
4. [Graph Model](#4-graph-model)
5. [Prerequisites & Setup](#5-prerequisites--setup)
6. [Running the Demo -- Step by Step](#6-running-the-demo--step-by-step)
   - [Phase 1: Setup](#phase-1-setup)
   - [Phase 2: First Upload -- Everything is v1](#phase-2-first-upload--everything-is-v1)
   - [Phase 3: View the Graph](#phase-3-view-the-graph)
   - [Phase 4: Version Change -- See the Delta](#phase-4-version-change--see-the-delta)
   - [Phase 5: Compare Before vs After](#phase-5-compare-before-vs-after)
7. [Neo4j Browser Queries -- What to Run at Each Step](#7-neo4j-browser-queries--what-to-run-at-each-step)
8. [All Available Commands](#8-all-available-commands)
9. [How the Impact Analyser Works](#9-how-the-impact-analyser-works)
10. [Current Limitations -- Demo vs Production](#10-current-limitations--demo-vs-production)
11. [Next Steps -- Production Grade](#11-next-steps--production-grade)

---

## 1. The Vision

The core idea behind this system came from one architectural principle:

> *"These are all independent uploads. There is nothing called a bulk upload of all things together. As and when something gets uploaded, we have to check with respect to the existing nodes, find out whether it is to be linked, and based on that, create the edge."*

In software testing today, test cases, API contracts, user stories, and system features are all managed in isolation -- different tools, different teams, no shared understanding of how they connect. When an API changes, nobody knows which test cases break. When a user story evolves, nobody knows which flows are affected.

This Knowledge Graph solves that by treating every entity -- a user story, a feature, an API endpoint, a test case -- as an **independent node**. User journeys are **`flows[]` on the story** (ordered feature names), not separate Flow nodes. The moment any node is uploaded, the system automatically scans all existing nodes and creates every valid relationship. No manual linking. No bulk imports. Order of upload does not matter.

**The standalone node principle:**

> *"If Mohsin is added first as manager, you have only one node -- no connections at all. Then when a new engineer is added, the edge is immediately created between them. Now imagine the reverse: the engineer was already in the system, then the manager joins -- at that point the same edge is created. The graph builds itself regardless of upload order."*

This is exactly how the Knowledge Graph works. Upload a user story first -- it stands alone (with optional `flows[]`). Upload features later -- the graph links them via `HAS_FEATURE` and `NEXT_STEP`. Upload features first -- they stand alone until the story arrives. **The graph is always consistent, always current.**

> **Sample data:** [`sample_data/README.md`](sample_data/README.md) · **edge coverage:** [`sample_data/DEMO_COVERAGE.md`](sample_data/DEMO_COVERAGE.md) · **where data is stored:** [`docs/STORAGE.md`](docs/STORAGE.md)

When an entity is updated (v2 uploaded):

> *"You have to know: is it a new one or is it a change to an existing one? If it is a change to an existing one, I just have to do an impact analysis of this node within the particular graph."*

The old version is **not deleted**. It is preserved with an expiry timestamp (`invalid_at`). The new version becomes active. All downstream nodes connected to the old version are surfaced as **impacted** -- needing review.

> *"If this is changing, the immediate impact is on these -- which means I have to regenerate the test cases. The impact depends on the level of coupling. If it is a tight coupling, a change at one node causes a cascading effect through the entire branch."*

The system tells you **what to act on** -- it does not act automatically. Whether the entity came from a pipeline, an LLM, or a human upload:

> *"Don't mix your pipeline and your knowledge graph in one tight coupling. The knowledge graph should have the interface to handle whenever a new entity comes in. Who provides you that entity -- that is a completely different thing."*

And when a new entity arrives, the scan is always three-directional:

> *"Any level that gets added, it needs to check the previous level above, and the level below, and the siblings -- and then associate. If nothing is there, just stand alone."*

---

## 2. What We Built

This is a **demonstration** of the above vision with real data from the Airtel Plan Change scenario.

### What is implemented

| Component | Description |
|-----------|-------------|
| **Node types** | UserStory, Feature, APIEndpoint, APIResponseSchema, TestCase |
| **Edges** | HAS_FEATURE, USES_API, HAS_RESPONSE_SCHEMA, NEXT_STEP, DEPENDS_ON, HAS_TEST_CASE, VALIDATES_AGAINST |
| **Flows** | `UserStory.flows[]` (ordered feature names) + `NEXT_STEP` between features — not separate nodes |
| **Full Versioning** | Every node: `base_id` (stable) + `node_id` (versioned e.g. `f1_v2`) + `valid_at` / `invalid_at` |
| **Relationship Mapper** | Runs after every upload -- scans above / below / siblings -- creates all matching edges automatically |
| **Impact Analyser** | Runs after every v2+ upload -- detects what changed, surfaces all impacted downstream nodes |
| **CLI** | `main.py` with commands for every operation |
| **Web UI + REST API** | Interactive graph at `http://localhost:9000` with live Neo4j CRUD |
| **Neo4j Browser** | Full visual graph -- before/after comparison queries provided |

### What this demo does NOT include

- No LLM -- relationships are declared via explicit fields in the JSON files (see [Section 10](#10-current-limitations--demo-vs-production))
- No test script generation pipeline
- No backend integration -- the graph is a standalone intelligence layer

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Knowledge Graph CLI                      │
│                        main.py                              │
└───────────────────────┬─────────────────────────────────────┘
                        │  every upload triggers all three
         ┌──────────────┼──────────────┐
         ▼              ▼              ▼
  graph_service    relationship_    impact_
  .py              mapper.py        analyser.py
  (CRUD +          (auto-link        (detect what
   versioning)      on upload)        changed + who
                                      is impacted)
         │
         ▼
    Neo4j Database
```

### Service files

| File | Responsibility |
|------|----------------|
| `services/graph_service.py` | Save / retrieve all node types. Versioning logic. Edge creation with `valid_at`. |
| `services/linking_engine.py` | Called after every upload. Scans existing nodes in 3 directions (above, below, siblings). Creates edges. |
| `docs/SCHEMA.md` | Canonical graph model (aligned with KnowledgeGraph_Schema docx) |
| `services/impact_analyser.py` | Called after every v2+ upload. Computes diff between old and new version. Returns structured list of impacted nodes. |
| `services/schema_service.py` | One-time Neo4j constraints and indexes setup. |
| `config.py` | Neo4j connection settings loaded from `.env`. |

---

## 4. Graph Model

### Node hierarchy

```
(:UserStory)  flows: ["Login", "PlanFetch", ...]   -- ordered journey (not separate nodes)
      │
      │ HAS_FEATURE
      ▼
(:Feature) ──NEXT_STEP──► (:Feature)     -- sequence from story.flows[]
      │
      │ USES_API
      ▼
(:APIEndpoint) ──HAS_RESPONSE_SCHEMA──► (:APIResponseSchema)

(:UserStory|Feature|APIEndpoint) ──HAS_TEST_CASE──► (:TestCase)
(:TestCase) ──VALIDATES_AGAINST──► (:APIResponseSchema)   [negative tests]
```

```
UserStory  -[HAS_FEATURE]->      Feature
UserStory  -[USES_API]->         APIEndpoint
UserStory  -[HAS_TEST_CASE]->    TestCase
Feature    -[USES_API]->         APIEndpoint
Feature    -[HAS_TEST_CASE]->    TestCase
APIEndpoint -[HAS_TEST_CASE]->   TestCase
```

**Flows** are a **list property** on `UserStory` (`flows[]`), not separate nodes. Feature order uses `NEXT_STEP` edges.

Full reference: [`docs/SCHEMA.md`](docs/SCHEMA.md), [`docs/STORAGE.md`](docs/STORAGE.md), [`sample_data/DEMO_COVERAGE.md`](sample_data/DEMO_COVERAGE.md).

### Node properties (all nodes carry these)

| Property | Description |
|----------|-------------|
| `base_id` | Stable ID -- never changes across versions. e.g. `"f1"`, `"Login"`, `"US1"` |
| `node_id` | Versioned ID -- e.g. `"f1_v1"`, `"f1_v2"` |
| `version` | Integer -- increments on each re-upload |
| `is_current` | `true` on the active version only |
| `valid_from` | ISO timestamp — when this version became active |
| `valid_to` | ISO timestamp — when superseded (`null` = still active) |
| `status` | `"active"` or `"archived"` — see `docs/VERSIONING.md` |

### Edge properties

| Property | Description |
|----------|-------------|
| `valid_from` | When this relationship was first created |
| `valid_to` | When this relationship was superseded (`null` = still active) |

---

## 5. Prerequisites & Setup

### Requirements

- Python 3.11+ (tested with Python 3.13)
- Neo4j Desktop or Neo4j AuraDB (free tier works)
- Python packages listed in `requirements.txt`

### Install

```bash
cd C:\Knowledge_graph_build
pip install -r requirements.txt
```

### Configure Neo4j connection

Copy `.env.example` to `.env` and fill in your credentials:

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_password
```

### Web UI (live graph editor)

Start the API server (serves the frontend and REST endpoints):

```bash
python3 run_server.py
# or: uvicorn api.main:app --reload --port 9000
```

Open **http://localhost:9000** in your browser.

| Action | Behavior |
|--------|----------|
| **Upload file** | Drop JSON/YAML (same files as CLI) — auto-detect type, preview, then sync to Neo4j |
| **Manual entry** | Collapsible backup form when you don't have a file |
| **Edit node** | Creates a new version in Neo4j (append-only); graph refreshes immediately |
| **Delete node** | Removes current version + relationships from Neo4j |
| **Story filter** | Dropdown scopes the view to one user story |

REST API: `GET /api/graph`, `POST /api/upload`, `POST /api/upload/preview`, `POST /api/nodes/{type}`, `DELETE /api/nodes/{type}/{id}`

### Python interpreter note (Windows)

If you have multiple Python installations, use the explicit path:

```bash
"C:\Users\<YourName>\AppData\Local\Programs\Python\Python313\python.exe" main.py <command>
```

---

## 6. Running the Demo -- Step by Step

The demo uses an **Airtel Plan Change** scenario:  
A subscriber wants to change their mobile plan -- they log in, view plans, switch plans, and make payment.

> **Key principle:** These commands can be run in any order.  
> The sequence below is the ideal order for a first-time clean demo.

---

### Phase 1: Setup

```bash
python main.py setup-schema
```

Creates Neo4j constraints and indexes. Run once. Safe to re-run.

---

### Phase 2: First Upload -- Everything is v1

Upload each entity independently. Watch how each upload auto-links to what already exists.

#### Step 1 -- Upload the User Story

```bash
python main.py upload-story sample_data/stories/story_v1.json
```

**What the file contains:**
```
"As a subscriber, I want to change my current mobile plan so that I can
 get better benefits. The user must login, view existing plan, browse
 recommended plans, select a new plan, make payment, and activate."
```

**What happens:**  
→ `US1_v1` node created  
→ No other nodes exist yet -- stands alone  
→ Relationship mapper runs, finds nothing to link

---

#### Step 2 -- Upload Features (independent -- can be uploaded in any order)

```bash
python main.py upload-feature sample_data/features/feature_login.json
python main.py upload-feature sample_data/features/feature_planfetch.json
python main.py upload-feature sample_data/features/feature_planswitch.json
python main.py upload-feature sample_data/features/feature_payment.json
```

**What happens:**  
→ 4 Feature nodes created -- each declares `apis_used` and optional `depends_on`  
→ No API nodes exist yet -- features stand alone  
→ Story already has `flows[]` -- when features exist, mapper creates `HAS_FEATURE` + `NEXT_STEP`

---

#### Step 3 -- Upload API Spec v1

```bash
python main.py upload-api sample_data/api/spec_v1.yaml
```

**What the spec contains (v1 -- password-based login):**
- `POST /auth/login` -- fields: `msisdn`, `password`
- `POST /auth/token/refresh`
- `GET /plans` -- parameterized: `?type=current` or `?type=recommended`
- `POST /plans/switch` -- fields: `msisdn`, `plan_id`
- `POST /payments/pay` -- fields: `order_id`, `payment_method`, `amount`
- `POST /payments/activate` -- fields: `order_id`

**What happens:**  
→ 6 APIEndpoint nodes created  
→ Linking engine links Features → APIs via `USES_API` and creates `APIResponseSchema` nodes from OpenAPI responses  
→ Re-links story `flows[]` → `HAS_FEATURE` + `NEXT_STEP` (Login → PlanFetch → PlanSwitch → Payment)

---

#### Step 4 -- Upload Test Cases

```bash
python main.py upload-testcase sample_data/testcases/TC-login-001.json
python main.py upload-testcase sample_data/testcases/TC-login-002.json
python main.py upload-testcase sample_data/testcases/TC-planfetch-001.json
python main.py upload-testcase sample_data/testcases/TC-planfetch-002.json
python main.py upload-testcase sample_data/testcases/TC-planswitch-001.json
python main.py upload-testcase sample_data/testcases/TC-planswitch-002.json
python main.py upload-testcase sample_data/testcases/TC-payment-001.json
python main.py upload-testcase sample_data/testcases/TC-payment-002.json
```

| Test Case | linked_to | Type | Title |
|-----------|-----------|------|-------|
| TC-login-001 | Login | positive | Valid login with correct credentials |
| TC-login-002 | Login | negative | Login with wrong password |
| TC-planfetch-001 | PlanFetch | positive | Fetch current and recommended plans |
| TC-planfetch-002 | PlanFetch | negative | Fetch plans without authentication |
| TC-planswitch-001 | PlanSwitch | positive | Switch to valid plan successfully |
| TC-planswitch-002 | PlanSwitch | negative | Switch plan with invalid plan id |
| TC-payment-001 | Payment | positive | Successful payment and plan activation |
| TC-payment-002 | Payment | negative | Payment with declined card |

**What happens:**  
→ Each TestCase links via `linked_to` (feature name, story id, or `METHOD:path`) using `HAS_TEST_CASE`

---

#### Step 5 -- View the complete graph (terminal)

```bash
python main.py show-graph US1
```

Expected output (features follow `flows[]` order; no Flow nodes):
```
📖 UserStory : US1  "Plan Change"  v1  flows: Login → PlanFetch → PlanSwitch → Payment

  ├── 🧩 Feature: Login  v1
  │      ├── 🔌 POST:/auth/login
  │      ├── 📋 TC-login-001 [positive]
  │      └── 📋 TC-login-002 [negative]
  ├── 🧩 Feature: PlanFetch  v1  (NEXT_STEP from Login)
  │      ├── 🔌 GET:/plans
  │      └── 📋 TC-planfetch-001 / TC-planfetch-002
  … PlanSwitch, Payment …
```

---

### Phase 3: View the Graph

Open Neo4j Browser at **http://localhost:7474**

**Full graph -- current versions only:**
```cypher
MATCH p=(us:UserStory {base_id:'US1', is_current:true})-[*1..6]->(n)
WHERE coalesce(n.is_current, true) = true
RETURN p
```

---

### Phase 4: Version Change -- See the Delta

The business requirements change. Login must now use OTP instead of password, promo codes are introduced, and a confirmation SMS is added.

#### Step 7 -- Upload story v2

```bash
python main.py upload-story sample_data/stories/story_v2.json
```

**What v2 adds:**  
`"...login using OTP-based authentication, view plan with validity details, browse plans with discount offers, select plan, apply a promo code, make payment, activate with a confirmation SMS."`

**What happens:**  
→ `US1_v1` expires (`invalid_at` set, `is_current = false`)  
→ `US1_v2` becomes active  
→ Story `flows[]` may be re-derived or kept from JSON  
→ **Impact report** -- features and test cases on this story flagged where content changed

---

#### Step 8 -- Upload API Spec v2

```bash
python main.py upload-api sample_data/api/spec_v2.yaml
```

**What changed in spec v2:**

| Endpoint | Change |
|----------|--------|
| `POST /auth/login` | `password` field removed → `otp` field added |
| `POST /plans/switch` | `promo_code` field added |
| `POST /payments/activate` | `notify_sms` field added |

**What happens per endpoint:**  
→ Old endpoint version expires, new version created  
→ **Impact report per endpoint** -- cascade traced automatically:  
  - API change → impacted Feature → impacted TestCases (via `linked_to`)

Example output for `/auth/login`:
```
IMPACT REPORT  --  API ENDPOINT: POST:/auth/login  (v1 --> v2)
  WHAT CHANGED:
    +  Fields Added: otp
    -  Fields Removed: password

  IMPACTED FEATURES (1): Login
  IMPACTED TEST CASES (2):
    !  TC-login-001  "Valid login with correct credentials"
    !  TC-login-002  "Login with wrong password"

  ACTION: review and re-upload affected test cases.
```

---

### Phase 5: Compare Before vs After

#### Terminal delta report

```bash
python main.py delta US1
```

Shows content word diff (old story vs new story) and all downstream nodes currently linked.

#### Full comparison with Neo4j queries

```bash
python main.py compare US1
```

Prints 4 ready-to-paste Neo4j Browser queries for visual before/after comparison.

---

## 7. Neo4j Browser Queries -- What to Run at Each Step

Open Neo4j Browser at **http://localhost:7474**

### After Phase 2 (v1 complete graph)

```cypher
-- Everything, current versions only
MATCH p=(us:UserStory {base_id:'US1', is_current:true})-[*1..6]->(n)
WHERE coalesce(n.is_current, true) = true
RETURN p
```

### After Phase 4 (before vs after)

```cypher
-- BEFORE: graph as it was under story v1
MATCH p=(old:UserStory {node_id:'US1_v1'})-[*1..6]->(n)
RETURN p
```

```cypher
-- AFTER: graph as it is under story v2
MATCH p=(new:UserStory {node_id:'US1_v2'})-[*1..6]->(n)
RETURN p
```

```cypher
-- BOTH TOGETHER: old and new nodes side by side in one view
-- Neo4j browser colours them differently
MATCH p=(us:UserStory {base_id:'US1'})-[*1..6]->(n)
RETURN p
```

### Edge timeline -- see valid_at / invalid_at on every relationship

```cypher
MATCH (a)-[r]->(b)
WHERE a.base_id = 'US1' OR b.base_id = 'US1'
RETURN a.node_id, type(r), r.valid_at, r.invalid_at, b.node_id
ORDER BY r.valid_at
```

### See all expired (historical) nodes

```cypher
MATCH (n)
WHERE n.is_current = false
  AND (n:UserStory OR n:Feature OR n:APIEndpoint OR n:APIResponseSchema OR n:TestCase)
RETURN labels(n)[0] AS type, n.node_id, n.valid_at, n.invalid_at
ORDER BY n.valid_at
```

### See test cases linked to expired features (impacted, not yet updated)

```cypher
MATCH (f:Feature {is_current:false})-[:HAS_TEST_CASE]->(tc:TestCase)
RETURN f.node_id AS expired_feature, tc.node_id AS tc, tc.title
ORDER BY expired_feature
```

### See all API schema changes across versions

```cypher
MATCH (ep:APIEndpoint)
WHERE ep.version > 1
RETURN ep.node_id, ep.method, ep.path, ep.valid_at, ep.invalid_at, ep.is_current
ORDER BY ep.path, ep.version
```

### See feature sequence (NEXT_STEP chain)

```cypher
MATCH p=(f:Feature {is_current:true})-[:NEXT_STEP*1..5]->(n:Feature {is_current:true})
WHERE f.base_id = 'Login'
RETURN p
```

### Count all nodes by type and version status

```cypher
MATCH (n)
WHERE n:UserStory OR n:Feature OR n:APIEndpoint OR n:APIResponseSchema OR n:TestCase
RETURN labels(n)[0]  AS type,
       n.is_current  AS is_current,
       count(n)      AS count
ORDER BY type, is_current DESC
```

---

## 8. All Available Commands

```bash
# One-time setup
python main.py setup-schema

# Upload any entity independently (any order, any time)
python main.py upload-story    sample_data/stories/story_v1.json
python main.py upload-feature  sample_data/features/feature_login.json
python main.py upload-api      sample_data/api/spec_v1.yaml
python main.py upload-testcase sample_data/testcases/TC-login-001.json

# Query the graph
python main.py show-graph    US1
python main.py show-history  story    US1
python main.py show-history  feature  Login
python main.py show-history  testcase TC-login-001

# Delta and comparison (after uploading a v2)
python main.py delta       US1
python main.py compare     US1
python main.py neo4j-query US1
```

---

## 9. How the Impact Analyser Works

Every time a **v2 or higher** upload happens, `impact_analyser.py` runs automatically after the relationship mapper. It compares the old and new version of the uploaded entity and traces all downstream nodes that are affected.

### Flow changed (e.g. f1 v1 → v2)

```
1. Diff features_used[]    → what features added / removed?
2. Diff depends_on[]       → what flow dependencies added / removed?
3. Diff step text          → did the actual test steps change?
4. TestCases linked to this flow (both versions)  → directly impacted
5. Flows that DEPEND_ON this flow                 → flagged
6. TestCases of those dependent flows             → indirectly impacted
7. Features in v2 not found in graph              → missing nodes warning
```

### Feature changed (e.g. Login v1 → v2)

```
1. Diff apis_used[]        → what APIs added / removed?
2. Flows using this feature                       → impacted flows
3. TestCases of those flows                       → impacted test cases
```

### API Endpoint changed (e.g. POST:/auth/login v1 → v2)

```
1. Diff request schema fields  → fields added / removed
2. Features that call this API                    → impacted features
3. Flows using those features                     → impacted flows
4. TestCases of those flows                       → impacted test cases
```

### UserStory changed (e.g. US1 v1 → v2)

```
1. Word-level diff on content  → concepts added / removed
2. All Flows under this story                     → all flagged
3. All TestCases of those flows                   → all flagged
```

### Example impact report output (on every v2+ upload)

```
IMPACT REPORT  --  FLOW: f1  (v1 --> v2)
──────────────────────────────────────────────────────────
  WHAT CHANGED:
    +  Features Added: OTPService
    ~  Flow steps updated (review test case steps)

  IMPACTED FLOWS (1) -- depend on this flow:
    !  f2_v1  "Fetch Current and Recommended Plans"

  IMPACTED TEST CASES (2) -- need review / re-upload:
    !  TC-f1-001_v1  "Valid login with correct credentials"
    !  TC-f1-002_v1  "Login with wrong password"

  INDIRECTLY IMPACTED TEST CASES (1) -- via dependent flows:
    ?  TC-f2-001_v1  [via flow f2]

  MISSING NODES -- features:
    +  OTPService  -- upload to complete the graph

  ACTION: 3 test case(s) flagged -- review and re-upload them.
──────────────────────────────────────────────────────────
```

---

## 10. Current Limitations -- Demo vs Production

### Relationship mapping is explicit in this demo

In this demo, each JSON file carries explicit cross-references so the relationship mapper knows what to link:

```json
// Flow declares its story and features
{
  "flow_id": "f1",
  "story_id": "US1",
  "features_used": ["Login"],
  "depends_on": []
}
```

```json
// Feature declares which API paths it calls
{
  "feature_id": "Login",
  "apis_used": ["/auth/login", "/auth/token/refresh"]
}
```

**In the real world, no one writes these IDs by hand.**

Real documents arrive as:
- User stories from JIRA / Confluence (plain text paragraphs)
- Feature descriptions from Word documents / Notion pages
- Flows generated by a pipeline from a user story (plain text, no explicit IDs)
- Test cases generated by an LLM (plain text, no explicit flow reference)

The relationship mapper needs to **infer** these links from the content, not read them from hardcoded fields.

### No pipeline integration

This graph is a standalone intelligence layer. In production, it will receive entities from:
- A test case generation pipeline (LLM-based)
- A CI/CD trigger when an API spec is merged
- A manual upload from a QA engineer
- An LLM that converts a Confluence page into a flow node

All of these are different producers. The graph does not care where the entity came from -- only that a new entity has arrived and needs to be linked.

---

## 11. Next Steps -- Production Grade

### Step 1 -- Replace explicit IDs with LLM-based relationship extraction

This is the single biggest change needed. Currently:

```
Upload flow JSON with "features_used": ["Login"]
→ mapper reads the field directly → creates edge
```

In production:

```
Upload flow as plain text description
→ LLM reads text + list of existing graph nodes
→ LLM returns { "story_id": "US1", "features_used": ["Login"] }
→ mapper creates the same edges as today
```

**What to build:** `services/llm_extractor.py`

```python
def extract_relationships(entity_type: str,
                          content: str,
                          existing_nodes: dict) -> dict:
    """
    Given plain-text content and a snapshot of existing graph nodes,
    call an LLM to identify which nodes this entity links to.
    Returns the same field structure used today:
      { story_id, features_used, depends_on, apis_used, flow_id, ... }
    """
    # 1. Build a prompt listing existing node names and base_ids
    # 2. Call Claude API using structured output / tool use
    # 3. Parse and validate the JSON response
    # 4. Return structured fields to the relationship mapper
```

**Only this one function changes.** The relationship mapper, graph service, impact analyser, versioning model, and Neo4j schema remain exactly as built in this demo.

**Recommended model:** Claude `claude-opus-4-7` via the Anthropic API.  
Use structured output (tool_use / JSON mode) to guarantee parseable responses.

**Which relationships need LLM vs which are deterministic:**

| Relationship | Method | Why |
|---|---|---|
| Feature → APIEndpoint | **Deterministic** | API paths are explicit in YAML |
| Flow → UserStory | **LLM** | Plain text, no explicit ID |
| Flow → Feature | **LLM** | Feature names must be matched semantically |
| TestCase → Flow | **LLM** | Test case text describes a flow, no ID |
| Flow → Flow (DEPENDS_ON) | **LLM** | Inferred from step ordering and references |

---

### Step 2 -- Handle unstructured entity inputs

Accept plain-text or document uploads instead of only JSON:

| Entity | Real-world source | Parsing approach |
|--------|------------------|-----------------|
| UserStory | JIRA ticket / Confluence page | LLM extraction |
| Feature | Word doc / Notion page | LLM extraction |
| APIEndpoint | OpenAPI YAML | Deterministic parser (keep as-is) |
| Flow | LLM-generated from story | Pipeline passes story_id; LLM extracts feature links |
| TestCase | LLM-generated from flow | Pipeline passes flow_id |

---

### Step 3 -- Graphiti evaluation

[Graphiti](https://github.com/getzep/graphiti) (by Zep) is an open-source library that:
- Accepts unstructured text as "episodes"
- Uses an LLM internally to extract entities and relationships
- Stores everything in Neo4j with temporal tracking (same `valid_at` / `invalid_at` pattern we use)

**Consider Graphiti for:** Free-text story and feature documents where entity boundaries are unclear and you want automatic entity discovery.

**Do not use Graphiti for:**
- API spec parsing -- keep the deterministic YAML parser
- Enforcing specific edge types -- Graphiti uses its own schema, which cannot guarantee `HAS_FLOW`, `USES_FEATURE`, `CALLS_API` etc.
- High-precision impact analysis -- the custom mapper gives full control over what gets linked and why

The custom relationship mapper built in this demo is more suited to this domain than Graphiti, because the entity types and relationship types are **well-defined and bounded**.

---

### Step 4 -- Confidence scoring on inferred relationships

When an LLM infers a relationship, attach a confidence score to the edge:

```cypher
// Edge created by LLM inference
(f1:Flow)-[:USES_FEATURE {
  confidence: 0.92,
  source: "llm_extract",
  model: "claude-opus-4-7",
  valid_at: "2026-05-24T..."
}]->(Login:Feature)
```

Low-confidence edges surface in the review UI before being used in impact analysis. High-confidence edges are applied automatically.

---

### Step 5 -- Approval and regeneration workflow

```
Impact report generated on upload
          │
          ▼
Human reviews impacted test cases in dashboard
          │
          ├── Approve regeneration
          │     → Pipeline picks up TC → LLM generates new test case
          │     → Uploaded as TC v2 → graph auto-links → old TC expires
          │
          └── Dismiss (loose coupling -- not actually impacted)
                → Dismissal stored as annotation on the edge
                → Not surfaced again for this change
```

---

### Step 6 -- REST API wrapper

Expose the graph as a REST API so any producer can send entities and query impact:

```
POST /upload/story       → returns { node_id, version, edges_created, impact }
POST /upload/feature
POST /upload/api
POST /upload/flow
POST /upload/testcase

GET  /graph/{story_id}              → full tree
GET  /impact/{entity_type}/{id}     → current impact report
GET  /history/{entity_type}/{id}    → version history
GET  /compare/{story_id}            → before/after Neo4j queries
```

The `main.py` CLI becomes a thin wrapper over this API.

---

### Step 7 -- Multi-story, cross-team impact

Currently the demo has one story (US1). In production, one API change can impact multiple stories across multiple teams:

```cypher
-- Find all user stories impacted by a change to POST:/auth/login
MATCH (ep:APIEndpoint {base_id:'POST:/auth/login'})<-[:CALLS_API]-(feat:Feature)
     <-[:USES_FEATURE]-(flow:Flow)<-[:HAS_FLOW]-(us:UserStory)
WHERE flow.is_current = true AND feat.is_current = true
RETURN DISTINCT us.base_id, us.title, collect(flow.base_id) AS affected_flows
```

The graph makes cross-team impact visible in a single query -- something no existing test management tool can do.

---

## Summary

```
Demo (this repo)                    Production (next steps)
────────────────────────────        ──────────────────────────────
Explicit JSON with IDs       →      Plain text + LLM extraction
Manual CLI uploads           →      REST API + pipeline triggers
Single story (US1)           →      Multi-story, multi-team
Field-based relationship     →      LLM-inferred + confidence score
  mapping
Terminal impact report       →      Dashboard + approval workflow
Neo4j Desktop                →      Neo4j AuraDB / self-hosted cluster
```

**The graph model, versioning, relationship mapper, and impact analyser built in this demo are production-ready architecture.** Only the input layer -- how entities arrive and how relationships are inferred from unstructured content -- needs to be upgraded for the real world.
