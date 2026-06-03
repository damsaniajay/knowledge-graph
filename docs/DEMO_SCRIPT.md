# Demo Script -- Step-by-Step Execution Guide

**Purpose:** Exact sequence of commands to run during the demo. Shows uploads in order, validates associations after each phase, and demonstrates version change with impact analysis.

**Duration:** ~15 minutes  
**Prerequisite:** Neo4j running, `.env` configured, `python main.py setup-schema` done once.

---

## Before You Start

```bash
cd C:\Knowledge_graph_build

# Verify Neo4j is reachable
python main.py setup-schema

# Open Neo4j Browser in a separate window
# URL: http://localhost:7474
# Login with your credentials
```

---

## PHASE 1 -- Upload v1 Entities (Fresh Graph)

> Goal: Show that each entity is independent. Each upload either stands alone or auto-links to what already exists.

---

### Step 1 -- Upload User Story

```bash
python main.py upload-story sample_data/stories/story_v1.json
```

**What to say:**  
"We upload the user story first. It stands alone -- no connections yet because no flows, features, or APIs exist."

**Neo4j -- paste after this step:**
```cypher
MATCH (n:UserStory) RETURN n
```
Expected: 1 node (US1_v1), no edges.

---

### Step 2 -- Upload Features

```bash
python main.py upload-feature sample_data/features/feature_login.json
python main.py upload-feature sample_data/features/feature_planfetch.json
python main.py upload-feature sample_data/features/feature_planswitch.json
python main.py upload-feature sample_data/features/feature_payment.json
```

**What to say:**  
"Four features uploaded independently. They stand alone -- no APIs or flows exist yet."

**Neo4j -- paste after this step:**
```cypher
MATCH (n:Feature) RETURN n
```
Expected: 4 Feature nodes, no edges yet.

---

### Step 3 -- Upload API Spec v1

```bash
python main.py upload-api sample_data/api/spec_v1.yaml
```

**What to say:**  
"Now we upload the API spec. Watch -- the moment these 6 endpoints are created, the system immediately finds the features that declared these paths and creates CALLS_API edges automatically."

**Neo4j -- paste after this step:**
```cypher
MATCH p=(f:Feature)-[:CALLS_API]->(ep:APIEndpoint)
RETURN p
```
Expected: 6 CALLS_API edges:
- Login → POST:/auth/login
- Login → POST:/auth/token/refresh
- PlanFetch → GET:/plans
- PlanSwitch → POST:/plans/switch
- Payment → POST:/payments/pay
- Payment → POST:/payments/activate

**Validate association explicitly:**
```cypher
MATCH (feat:Feature {name:'PlanFetch'})-[:CALLS_API]->(ep:APIEndpoint)
RETURN feat.name AS feature, ep.method + ' ' + ep.path AS api
```
Expected: `PlanFetch | GET /plans`

---

### Step 4 -- Upload Flows

```bash
python main.py upload-flow sample_data/flows/f1_login.json
python main.py upload-flow sample_data/flows/f2_fetchplan.json
python main.py upload-flow sample_data/flows/f3_planswitch.json
python main.py upload-flow sample_data/flows/f4_payment.json
```

**What to say:**  
"Each flow upload triggers the relationship mapper in 4 directions: link up to the story, link across to features, link to sibling flows via DEPENDS_ON, and link down to any test cases already in the graph."

**Neo4j -- paste after each flow to show the chain building:**
```cypher
MATCH p=(us:UserStory {base_id:'US1', is_current:true})-[*1..4]->(n)
WHERE coalesce(n.is_current, true) = true
RETURN p
```

**Validate association -- Plan Change → Plan Fetch path:**
```cypher
MATCH path = (us:UserStory {base_id:'US1'})-[:HAS_FLOW]->(f:Flow)-[:USES_FEATURE]->(feat:Feature {name:'PlanFetch'})
WHERE us.is_current = true AND f.is_current = true
RETURN us.title AS story, f.title AS flow, feat.name AS feature
```
Expected: `Plan Change | Fetch Current and Recommended Plans | PlanFetch`

**Validate dependency chain:**
```cypher
MATCH p=(f:Flow {is_current:true})-[:DEPENDS_ON*1..4]->(dep:Flow {is_current:true})
RETURN p
```
Expected: f2→f1, f3→f2, f4→f3

---

### Step 5 -- Upload Test Cases

```bash
python main.py upload-testcase sample_data/testcases/TC-f1-001.json
python main.py upload-testcase sample_data/testcases/TC-f1-002.json
python main.py upload-testcase sample_data/testcases/TC-f2-001.json
python main.py upload-testcase sample_data/testcases/TC-f3-001.json
python main.py upload-testcase sample_data/testcases/TC-f4-001.json
```

**What to say:**  
"Test cases uploaded. Each links to its parent flow. Notice f1 has two test cases -- one positive (correct credentials) and one negative (wrong password). The other flows currently have only positive test cases -- the system will flag those as coverage gaps."

**Neo4j -- full graph with test cases:**
```cypher
MATCH p=(us:UserStory {base_id:'US1', is_current:true})-[*1..6]->(n)
WHERE coalesce(n.is_current, true) = true
RETURN p
```

**Validate test case coverage:**
```cypher
MATCH (f:Flow {is_current:true})
OPTIONAL MATCH (f)-[:HAS_TEST_CASE]->(pos:TestCase {type:'positive', is_current:true})
OPTIONAL MATCH (f)-[:HAS_TEST_CASE]->(neg:TestCase {type:'negative', is_current:true})
RETURN f.base_id AS flow,
       f.title AS title,
       count(DISTINCT pos) AS positive_tcs,
       count(DISTINCT neg) AS negative_tcs,
       CASE WHEN count(DISTINCT neg) = 0 THEN 'COVERAGE GAP -- no negative TC' ELSE 'OK' END AS status
ORDER BY flow
```

---

### Step 6 -- Show Full Graph (Terminal)

```bash
python main.py show-graph US1
```

**What to say:**  
"This shows the full tree. Story at the top, flows underneath, features and APIs below each flow, test cases at the leaves. Every node at every level is versioned."

---

## PHASE 2 -- Validate Associations Explicitly

> Goal: Prove each edge was created. Show that the system does not assume -- it validates.

**Run all 5 validation queries in Neo4j Browser:**

```cypher
-- 1. UserStory → Flows
MATCH (us:UserStory {base_id:'US1', is_current:true})-[:HAS_FLOW]->(f:Flow {is_current:true})
RETURN us.base_id AS story, collect(f.base_id) AS flows
```

```cypher
-- 2. Flows → Features
MATCH (f:Flow {is_current:true})-[:USES_FEATURE]->(feat:Feature {is_current:true})
RETURN f.base_id AS flow, collect(feat.name) AS features
ORDER BY flow
```

```cypher
-- 3. Features → APIs
MATCH (feat:Feature {is_current:true})-[:CALLS_API]->(ep:APIEndpoint {is_current:true})
RETURN feat.name AS feature, collect(ep.method + ' ' + ep.path) AS apis
ORDER BY feature
```

```cypher
-- 4. Flows → Test Cases (with type)
MATCH (f:Flow {is_current:true})-[:HAS_TEST_CASE]->(tc:TestCase {is_current:true})
RETURN f.base_id AS flow, tc.type AS type, tc.base_id AS tc_id, tc.title AS title
ORDER BY flow, type
```

```cypher
-- 5. Full path: Story → Flow → Feature → API (end-to-end)
MATCH path = (us:UserStory {base_id:'US1', is_current:true})
             -[:HAS_FLOW]->(f:Flow {is_current:true})
             -[:USES_FEATURE]->(feat:Feature {is_current:true})
             -[:CALLS_API]->(ep:APIEndpoint {is_current:true})
RETURN us.title AS story,
       f.title  AS flow,
       feat.name AS feature,
       ep.method + ' ' + ep.path AS api
ORDER BY flow
```

---

## PHASE 3 -- Version Change and Impact Analysis

> Goal: Upload v2 entities and show the system detects exactly what changed and which downstream nodes are impacted.

---

### Step 7 -- Upload Story v2

```bash
python main.py upload-story sample_data/stories/story_v2.json
```

**What changed:** Login now uses OTP, promo code added, confirmation SMS added.

**What to say:**  
"Story is updated. Watch the impact report -- it tells us all flows and test cases connected to this story are potentially impacted because the business requirement changed."

**Neo4j -- see both versions:**
```cypher
MATCH (us:UserStory {base_id:'US1'})
RETURN us.node_id, us.version, us.is_current, us.valid_at, us.invalid_at, us.status
ORDER BY us.version
```

---

### Step 8 -- Upload API Spec v2

```bash
python main.py upload-api sample_data/api/spec_v2.yaml
```

**What changed:**
- `POST /auth/login` -- `password` field removed, `otp` field added
- `POST /plans/switch` -- `promo_code` field added
- `POST /payments/activate` -- `notify_sms` field added

**What to say:**  
"For each API endpoint that changed, the system automatically traces the full cascade: API → Feature → Flow → TestCase. It tells you exactly which test cases need to be reviewed for each schema change."

**Neo4j -- see API version history:**
```cypher
MATCH (ep:APIEndpoint)
WHERE ep.path IN ['/auth/login', '/plans/switch', '/payments/activate']
RETURN ep.node_id, ep.path, ep.version, ep.is_current, ep.valid_at
ORDER BY ep.path, ep.version
```

---

### Step 9 -- Upload Flow v2 (Login with OTP)

```bash
python main.py upload-flow sample_data/flows/f1_login_v2.json
```

**What changed:** `features_used` now includes `OTPService` (not yet in graph).

**What to say:**  
"Flow f1 is updated. The system detects: OTPService feature is referenced but not yet uploaded -- missing node warning. TC-f1-001 and TC-f1-002 are directly impacted. TC-f2-001 is indirectly impacted because f2 depends on f1."

**Neo4j -- see f1 history:**
```cypher
MATCH (f:Flow {base_id:'f1'})
RETURN f.node_id, f.version, f.is_current, f.features_used, f.valid_at, f.invalid_at
ORDER BY f.version
```

---

## PHASE 4 -- Compare Before vs After

### Step 10 -- Terminal comparison

```bash
python main.py compare US1
```

### Step 11 -- Visual comparison in Neo4j Browser

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
-- BOTH TOGETHER: old and new nodes in one view
MATCH p=(us:UserStory {base_id:'US1'})-[*1..6]->(n)
RETURN p
```

```cypher
-- EDGE TIMELINE: when each relationship was created / expired
MATCH (a)-[r]->(b)
WHERE a.base_id = 'US1' OR b.base_id = 'US1'
RETURN a.node_id, type(r), r.valid_at, r.invalid_at, b.node_id
ORDER BY r.valid_at
```

**What to say:**  
"This is the full history of the graph. Every node that existed, every edge that was created, and exactly when each version became active or expired. Nothing is lost -- the entire evolution is traceable."

---

## Summary Table -- What Each Phase Demonstrates

| Phase | What it shows |
|-------|--------------|
| Phase 1 | Independent uploads, auto-linking in all directions |
| Phase 2 | Association validation -- every edge proven to exist |
| Phase 3 | Impact cascade -- API/story change automatically surfaces all affected test cases |
| Phase 4 | Full temporal history -- before/after visual in Neo4j Browser |
