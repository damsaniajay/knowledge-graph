# Knowledge Graph -- Formal Schema Definition

**Version:** 1.0  
**Status:** Awaiting Review  
**Purpose:** Define all node types, properties, edge types, cardinality rules, and constraints before implementation proceeds.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Node Types](#2-node-types)
3. [Edge Types (Relationships)](#3-edge-types-relationships)
4. [Cardinality Rules](#4-cardinality-rules)
5. [Versioning Model](#5-versioning-model)
6. [Validation Rules](#6-validation-rules)
7. [Association Rules -- What Links to What](#7-association-rules----what-links-to-what)
8. [Neo4j Constraints and Indexes](#8-neo4j-constraints-and-indexes)

---

## 1. Overview

### Graph structure

```
(:UserStory)
      |
      | HAS_FLOW
      v
(:Flow) --------DEPENDS_ON--------> (:Flow)
      |
      | USES_FEATURE
      v
(:Feature)
      |
      | CALLS_API
      v
(:APIEndpoint)

(:Flow) ---HAS_TEST_CASE---> (:TestCase)
```

### Design principles

| Principle | Description |
|-----------|-------------|
| **Independent uploads** | Every entity is uploaded separately, in any order, at any time |
| **Auto-association** | On every upload the system scans existing nodes and creates all valid edges automatically |
| **Full versioning** | No node is ever deleted. A new upload creates v(n+1) and expires v(n) |
| **Impact tracing** | Every v2+ upload triggers an impact report showing all downstream nodes affected |
| **Separation of concerns** | The knowledge graph is decoupled from all pipelines and generators. It only cares that an entity arrived |

---

## 2. Node Types

### 2.1 UserStory

Represents a business requirement. The top-level node in the hierarchy.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `base_id` | string | YES | Stable identifier. Never changes across versions. e.g. `"US1"` |
| `node_id` | string | YES | Versioned identifier. e.g. `"US1_v1"`, `"US1_v2"` |
| `title` | string | YES | Short name. e.g. `"Plan Change"` |
| `content` | string | YES | Full business requirement text |
| `version` | integer | YES | Starts at 1, increments on each re-upload |
| `is_current` | boolean | YES | `true` on active version only. Exactly one version is current at any time |
| `valid_at` | datetime (ISO 8601) | YES | When this version became active |
| `invalid_at` | datetime (ISO 8601) | NO | When this version was superseded. `null` if still active |
| `status` | enum | YES | `"active"` or `"expired"` |
| `created_by` | string | NO | Source of upload: `"system"`, `"pipeline"`, or user ID |
| `created_at` | datetime (ISO 8601) | YES | When this node was inserted |

**Uniqueness constraint:** `node_id` must be unique across all nodes.  
**Index:** `base_id`, for fast lookup of all versions.

---

### 2.2 Feature

Represents a named system capability. Examples: Login, Payment, PlanFetch, PlanSwitch.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `base_id` | string | YES | Stable identifier. e.g. `"Login"` |
| `node_id` | string | YES | Versioned identifier. e.g. `"Login_v1"` |
| `name` | string | YES | Human-readable name. Used for matching with `Flow.features_used[]` |
| `description` | string | NO | What this feature does |
| `apis_used` | string[] | YES | List of API paths this feature calls. e.g. `["/auth/login", "/auth/token/refresh"]` |
| `version` | integer | YES | Starts at 1 |
| `is_current` | boolean | YES | `true` on active version only |
| `valid_at` | datetime | YES | When this version became active |
| `invalid_at` | datetime | NO | `null` if still active |
| `status` | enum | YES | `"active"` or `"expired"` |
| `created_by` | string | NO | Source |
| `created_at` | datetime | YES | Insert timestamp |

**Uniqueness constraint:** `node_id` must be unique.  
**Index:** `base_id`, `name` (used for matching).

---

### 2.3 APIEndpoint

Represents one REST API endpoint from an OpenAPI specification.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `base_id` | string | YES | Stable identifier. Format: `"METHOD:path"`. e.g. `"POST:/auth/login"` |
| `node_id` | string | YES | Versioned identifier. e.g. `"POST:/auth/login_v1"` |
| `path` | string | YES | URL path. e.g. `"/auth/login"` |
| `method` | enum | YES | `"GET"`, `"POST"`, `"PUT"`, `"DELETE"`, `"PATCH"` |
| `summary` | string | NO | Short description from OpenAPI spec |
| `request_schema` | JSON string | NO | OpenAPI request body schema (serialized as JSON string) |
| `response_schema` | JSON string | NO | OpenAPI response schema (serialized as JSON string) |
| `version` | integer | YES | Starts at 1 |
| `is_current` | boolean | YES | `true` on active version only |
| `valid_at` | datetime | YES | When this version became active |
| `invalid_at` | datetime | NO | `null` if still active |
| `status` | enum | YES | `"active"` or `"expired"` |
| `created_by` | string | NO | Source |
| `created_at` | datetime | YES | Insert timestamp |

**Uniqueness constraint:** `node_id` must be unique.  
**Index:** `base_id`, `path` (used for matching from Feature.apis_used).

---

### 2.4 Flow

Represents one step in a user journey derived from a UserStory. Examples: User Login, Fetch Plans, Switch Plan, Payment.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `base_id` | string | YES | Stable identifier. e.g. `"f1"`, `"f2"` |
| `node_id` | string | YES | Versioned identifier. e.g. `"f1_v1"`, `"f1_v2"` |
| `story_id` | string | YES | `base_id` of the parent UserStory. Used to create `HAS_FLOW` edge |
| `title` | string | YES | Short name. e.g. `"User Login"` |
| `description` | string | NO | What this flow does |
| `steps` | string[] | YES | Ordered list of steps the user/system performs |
| `features_used` | string[] | YES | List of Feature names used in this flow. e.g. `["Login"]` |
| `depends_on` | string[] | NO | List of Flow `base_id`s that must complete before this flow. e.g. `["f1"]` |
| `version` | integer | YES | Starts at 1 |
| `is_current` | boolean | YES | `true` on active version only |
| `valid_at` | datetime | YES | When this version became active |
| `invalid_at` | datetime | NO | `null` if still active |
| `status` | enum | YES | `"active"` or `"expired"` |
| `created_by` | string | NO | Source |
| `created_at` | datetime | YES | Insert timestamp |

**Uniqueness constraint:** `node_id` must be unique.  
**Index:** `base_id`, `story_id` (used for matching).

---

### 2.5 TestCase

Represents a specific test scenario for a Flow. Every Flow must have at least one positive AND one negative test case.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `base_id` | string | YES | Stable identifier. e.g. `"TC-f1-001"` |
| `node_id` | string | YES | Versioned identifier. e.g. `"TC-f1-001_v1"` |
| `flow_id` | string | YES | `base_id` of the parent Flow. Used to create `HAS_TEST_CASE` edge |
| `title` | string | YES | Short description of what is being tested |
| `type` | enum | YES | `"positive"`, `"negative"`, or `"edge_case"` |
| `steps` | string[] | YES | Ordered test steps |
| `expected_result` | string | YES | What the system should return or do |
| `version` | integer | YES | Starts at 1 |
| `is_current` | boolean | YES | `true` on active version only |
| `valid_at` | datetime | YES | When this version became active |
| `invalid_at` | datetime | NO | `null` if still active |
| `status` | enum | YES | `"active"` or `"expired"` |
| `created_by` | string | NO | Source |
| `created_at` | datetime | YES | Insert timestamp |

**Uniqueness constraint:** `node_id` must be unique.  
**Index:** `base_id`, `flow_id` (used for matching).

**Coverage rule:** Every Flow must have at minimum:
- 1 TestCase with `type = "positive"`
- 1 TestCase with `type = "negative"`

---

## 3. Edge Types (Relationships)

All edges carry `valid_at` and `invalid_at` timestamps matching the versioning model of nodes.

### 3.1 HAS_FLOW

```
(:UserStory) -[:HAS_FLOW]-> (:Flow)
```

| Property | Value |
|----------|-------|
| Direction | UserStory → Flow |
| Cardinality | One UserStory to Many Flows |
| Created when | `Flow.story_id` matches `UserStory.base_id` |
| Created by | `relationship_mapper` on Flow upload OR UserStory upload |
| `valid_at` | Timestamp when the edge was first created |
| `invalid_at` | Set when either endpoint node is expired and a new version is linked |

**Validation:**
- `story_id` on the Flow must reference an existing `UserStory.base_id`
- If `story_id` references a non-existent UserStory, the Flow is created but no edge is made. A warning is emitted: `"UserStory <story_id> not found -- HAS_FLOW edge pending"`

---

### 3.2 USES_FEATURE

```
(:Flow) -[:USES_FEATURE]-> (:Feature)
```

| Property | Value |
|----------|-------|
| Direction | Flow → Feature |
| Cardinality | Many Flows to Many Features |
| Created when | A name in `Flow.features_used[]` matches `Feature.name` |
| Created by | `relationship_mapper` on Flow upload OR Feature upload |
| `valid_at` | Timestamp when the edge was first created |

**Validation:**
- Each name in `features_used[]` is checked against existing `Feature.name` values
- Unmatched names are flagged as MISSING NODES and printed in the impact/upload report

---

### 3.3 CALLS_API

```
(:Feature) -[:CALLS_API]-> (:APIEndpoint)
```

| Property | Value |
|----------|-------|
| Direction | Feature → APIEndpoint |
| Cardinality | Many Features to Many APIEndpoints |
| Created when | A path in `Feature.apis_used[]` matches `APIEndpoint.path` |
| Created by | `relationship_mapper` on Feature upload OR APIEndpoint upload |
| `valid_at` | Timestamp when the edge was first created |

**Validation:**
- Each path in `apis_used[]` is checked against existing `APIEndpoint.path` values
- Unmatched paths are flagged as MISSING NODES

---

### 3.4 HAS_TEST_CASE

```
(:Flow) -[:HAS_TEST_CASE]-> (:TestCase)
```

| Property | Value |
|----------|-------|
| Direction | Flow → TestCase |
| Cardinality | One Flow to Many TestCases |
| Created when | `TestCase.flow_id` matches `Flow.base_id` |
| Created by | `relationship_mapper` on TestCase upload OR Flow upload |
| `valid_at` | Timestamp when the edge was first created |

**Validation:**
- `flow_id` must reference an existing `Flow.base_id`
- If `flow_id` references a non-existent Flow, the TestCase is created but no edge is made. Warning emitted
- System checks if the linked Flow has both positive and negative test cases. If only positive exists, a warning is printed: `"Flow <flow_id> has no negative test case -- coverage gap"`

---

### 3.5 DEPENDS_ON

```
(:Flow) -[:DEPENDS_ON]-> (:Flow)
```

| Property | Value |
|----------|-------|
| Direction | Flow → Flow (sibling) |
| Cardinality | Many to Many (but must be acyclic) |
| Created when | A `base_id` in `Flow.depends_on[]` matches another `Flow.base_id` |
| Created by | `relationship_mapper` on Flow upload |
| `valid_at` | Timestamp when the edge was first created |

**Validation:**
- Each `base_id` in `depends_on[]` must reference an existing `Flow.base_id`
- Circular dependencies must be detected and rejected: if A depends_on B and B depends_on A, the second upload is rejected with error: `"Circular dependency detected: <flow_id> -> <dep_id> creates a cycle"`
- A Flow cannot list itself in `depends_on[]`

---

## 4. Cardinality Rules

| Relationship | From | To | Cardinality |
|---|---|---|---|
| HAS_FLOW | UserStory | Flow | 1 : N (one story, many flows) |
| USES_FEATURE | Flow | Feature | M : N (many flows can use same feature) |
| CALLS_API | Feature | APIEndpoint | M : N (many features can call same API) |
| HAS_TEST_CASE | Flow | TestCase | 1 : N (one flow, many test cases) |
| DEPENDS_ON | Flow | Flow | M : N (acyclic -- must not form a cycle) |

---

## 5. Versioning Model

Every upload creates a new node version. The old version is preserved, never deleted.

### Version lifecycle

```
State 1: First upload
  US1_v1 [ is_current=true,  valid_at=T1, invalid_at=null,  status=active ]

State 2: Re-upload (new version)
  US1_v1 [ is_current=false, valid_at=T1, invalid_at=T2,    status=expired ]
  US1_v2 [ is_current=true,  valid_at=T2, invalid_at=null,  status=active  ]
```

### Rules

1. At any point in time, exactly **one version** of any `base_id` has `is_current = true`
2. When a new version is uploaded, the system:
   a. Sets `is_current = false` and `invalid_at = now()` on the previous version
   b. Creates the new node with `is_current = true`, `valid_at = now()`, `invalid_at = null`
   c. Runs `relationship_mapper` to create edges for the new version
   d. Runs `impact_analyser` to surface all downstream nodes affected
3. Old nodes are **never deleted** -- they are the historical record
4. Edges also carry `valid_at` and `invalid_at` for full temporal tracking

### Node ID format

| Entity | base_id format | node_id format | Example |
|--------|---------------|----------------|---------|
| UserStory | Free string | `{base_id}_v{n}` | `US1_v2` |
| Feature | Feature name | `{base_id}_v{n}` | `Login_v1` |
| APIEndpoint | `{METHOD}:{path}` | `{base_id}_v{n}` | `POST:/auth/login_v2` |
| Flow | Short code | `{base_id}_v{n}` | `f1_v3` |
| TestCase | `TC-{flow_id}-{seq}` | `{base_id}_v{n}` | `TC-f1-001_v1` |

---

## 6. Validation Rules

### 6.1 Upload-time validation (required fields)

| Entity | Required fields | Rejected if missing |
|--------|----------------|---------------------|
| UserStory | `story_id`, `title`, `content` | Yes -- upload fails |
| Feature | `feature_id`, `name`, `apis_used` | Yes -- upload fails |
| APIEndpoint | `path`, `method` | Yes -- upload fails |
| Flow | `flow_id`, `title`, `story_id`, `features_used`, `steps` | Yes -- upload fails |
| TestCase | `tc_id`, `title`, `flow_id`, `type`, `steps`, `expected_result` | Yes -- upload fails |

### 6.2 Association validation (warnings, not rejections)

These do not block the upload but are printed as explicit warnings:

| Scenario | Warning message |
|----------|----------------|
| Flow.story_id not found in graph | `WARN: UserStory '{story_id}' not found -- HAS_FLOW edge pending` |
| Flow.features_used[] name not found | `WARN: Feature '{name}' not found -- USES_FEATURE edge pending. Upload the feature to complete the link` |
| Flow.depends_on[] flow not found | `WARN: Flow '{base_id}' not found -- DEPENDS_ON edge pending` |
| Feature.apis_used[] path not found | `WARN: APIEndpoint '{path}' not found -- CALLS_API edge pending. Upload the API spec to complete the link` |
| TestCase.flow_id not found | `WARN: Flow '{flow_id}' not found -- HAS_TEST_CASE edge pending` |
| Flow has no negative test case | `WARN: Flow '{flow_id}' has no negative test case -- coverage gap detected` |
| Flow has no positive test case | `WARN: Flow '{flow_id}' has no positive test case -- coverage gap detected` |

### 6.3 Structural validation (rejections)

| Scenario | Error message |
|----------|--------------|
| Circular DEPENDS_ON | `ERROR: Circular dependency detected: {flow_id} -> {dep_id} would create a cycle. Upload rejected` |
| Flow depends_on itself | `ERROR: Flow '{flow_id}' cannot depend on itself` |
| Duplicate node_id | `ERROR: node_id '{node_id}' already exists. This should not happen -- check versioning logic` |

---

## 7. Association Rules -- What Links to What

This section defines the exact logic the `relationship_mapper` follows on every upload.

### When a UserStory is uploaded

```
Scan direction: DOWN
  - Find all Flows where Flow.story_id == this UserStory.base_id
  - For each match: create (UserStory)-[:HAS_FLOW]->(Flow)
```

### When a Feature is uploaded

```
Scan direction: UP (from flows) + DOWN (to APIs)

UP:
  - Find all Flows where this Feature.name is in Flow.features_used[]
  - For each match: create (Flow)-[:USES_FEATURE]->(Feature)

DOWN:
  - For each path in this Feature.apis_used[]:
    - Find APIEndpoint where APIEndpoint.path == path
    - If found: create (Feature)-[:CALLS_API]->(APIEndpoint)
    - If not found: emit MISSING NODE warning
```

### When an APIEndpoint is uploaded

```
Scan direction: UP (from features)
  - Find all Features where this APIEndpoint.path is in Feature.apis_used[]
  - For each match: create (Feature)-[:CALLS_API]->(APIEndpoint)
```

### When a Flow is uploaded

```
Scan direction: UP + ACROSS (features) + SIBLINGS (depends_on) + DOWN (test cases)

UP:
  - Find UserStory where UserStory.base_id == this Flow.story_id
  - If found: create (UserStory)-[:HAS_FLOW]->(Flow)
  - If not found: emit MISSING NODE warning

ACROSS (features):
  - For each name in this Flow.features_used[]:
    - Find Feature where Feature.name == name
    - If found: create (Flow)-[:USES_FEATURE]->(Feature)
    - If not found: emit MISSING NODE warning

SIBLINGS (depends_on):
  - For each base_id in this Flow.depends_on[]:
    - Detect circular dependency -- reject if cycle detected
    - Find Flow where Flow.base_id == base_id
    - If found: create (Flow)-[:DEPENDS_ON]->(dep_Flow)
    - If not found: emit MISSING NODE warning

DOWN (test cases):
  - Find all TestCases where TestCase.flow_id == this Flow.base_id
  - For each match: create (Flow)-[:HAS_TEST_CASE]->(TestCase)
  - Check positive/negative coverage -- emit warning if either is missing
```

### When a TestCase is uploaded

```
Scan direction: UP (to flow)
  - Find Flow where Flow.base_id == this TestCase.flow_id
  - If found: create (Flow)-[:HAS_TEST_CASE]->(TestCase)
  - If not found: emit MISSING NODE warning
  - After linking: re-check coverage for the parent Flow
    - If Flow now has both positive and negative: emit "Coverage complete for Flow {flow_id}"
    - If still missing one type: emit coverage gap warning
```

---

## 8. Neo4j Constraints and Indexes

### Uniqueness constraints (enforced by Neo4j)

```cypher
CREATE CONSTRAINT FOR (n:UserStory)   REQUIRE n.node_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:Feature)     REQUIRE n.node_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:APIEndpoint) REQUIRE n.node_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:Flow)        REQUIRE n.node_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:TestCase)    REQUIRE n.node_id IS UNIQUE;
```

### Indexes for fast lookup

```cypher
CREATE INDEX FOR (n:UserStory)   ON (n.base_id);
CREATE INDEX FOR (n:Feature)     ON (n.base_id);
CREATE INDEX FOR (n:Feature)     ON (n.name);
CREATE INDEX FOR (n:APIEndpoint) ON (n.base_id);
CREATE INDEX FOR (n:APIEndpoint) ON (n.path);
CREATE INDEX FOR (n:Flow)        ON (n.base_id);
CREATE INDEX FOR (n:Flow)        ON (n.story_id);
CREATE INDEX FOR (n:TestCase)    ON (n.base_id);
CREATE INDEX FOR (n:TestCase)    ON (n.flow_id);
```

### Index for current-version fast access

```cypher
CREATE INDEX FOR (n:UserStory)   ON (n.base_id, n.is_current);
CREATE INDEX FOR (n:Feature)     ON (n.base_id, n.is_current);
CREATE INDEX FOR (n:APIEndpoint) ON (n.base_id, n.is_current);
CREATE INDEX FOR (n:Flow)        ON (n.base_id, n.is_current);
CREATE INDEX FOR (n:TestCase)    ON (n.base_id, n.is_current);
```
