# Modeling Layer -- Neo4j + LLM Architecture

**Purpose:** Define the architecture of the layer that sits between the Neo4j knowledge graph and the LLM. The LLM never queries raw Neo4j data directly. Instead, the modeling layer provides structured context, schema metadata, and formatted graph excerpts for RAG operations.

---

## Why a Modeling Layer?

The raw Neo4j graph contains nodes and edges with properties. An LLM given direct access to this raw data would have no understanding of:
- What a `HAS_FLOW` edge *means* in the context of this domain
- Why `is_current = false` nodes still matter (history)
- How to reason about impact: "if this API changes, which test cases are affected?"
- What the difference is between a `base_id` and a `node_id`

The modeling layer solves this by wrapping the graph data in rich contextual metadata before it ever reaches the LLM.

---

## Architecture

```
+-----------------------+
|   User / Pipeline     |  "What is impacted if POST:/auth/login changes?"
+-----------+-----------+
            |
            v
+-----------+-----------+
|    Modeling Layer     |
|                       |
|  1. Schema Context    |  What each node/edge type means
|  2. Context Builder   |  Fetches relevant subgraph from Neo4j
|  3. Prompt Builder    |  Formats graph data as LLM context
+-----------+-----------+
            |
     +------+------+
     |             |
     v             v
+---------+   +---------+
|  Neo4j  |   |   LLM   |
| (graph) |   | (Claude)|
+---------+   +---------+
```

---

## Component 1 -- Schema Context

A static metadata file (or database record) that describes every node type and edge type in plain English. This is injected into every LLM prompt.

### Node type definitions

```json
{
  "node_types": {
    "UserStory": {
      "description": "A business requirement that describes what the system must do from the user's perspective. It is the root of the knowledge graph. All flows, features, APIs, and test cases ultimately trace back to a UserStory.",
      "key_properties": ["base_id", "title", "content", "version", "is_current"],
      "example": "US1 -- Plan Change: As a subscriber I want to change my mobile plan"
    },
    "Feature": {
      "description": "A named system capability. Features are cross-cutting -- the same Feature (e.g. Login) can be used by multiple Flows across multiple UserStories. A Feature declares which API endpoints it calls.",
      "key_properties": ["base_id", "name", "apis_used", "version"],
      "example": "Login -- calls /auth/login and /auth/token/refresh"
    },
    "APIEndpoint": {
      "description": "A single REST API endpoint. It carries the full request/response schema. When the schema changes (fields added or removed), all Features that call this API and all Flows and TestCases downstream are potentially impacted.",
      "key_properties": ["base_id", "path", "method", "request_schema", "version"],
      "example": "POST:/auth/login -- request fields: msisdn, otp"
    },
    "Flow": {
      "description": "A specific step in a user journey, derived from a UserStory. A Flow uses one or more Features and may depend on other Flows completing first. Every Flow must have at least one positive and one negative TestCase.",
      "key_properties": ["base_id", "story_id", "title", "features_used", "depends_on", "steps", "version"],
      "example": "f1 -- User Login: depends on nothing, uses Login feature"
    },
    "TestCase": {
      "description": "A specific test scenario for a Flow. Can be positive (happy path) or negative (failure/error path). When a Flow or its upstream dependencies change, its TestCases must be reviewed and potentially regenerated.",
      "key_properties": ["base_id", "flow_id", "title", "type", "steps", "expected_result", "version"],
      "example": "TC-f1-002 -- negative: Login with wrong password, expect HTTP 401"
    }
  },
  "relationship_types": {
    "HAS_FLOW": "A UserStory contains this Flow as part of its implementation. If the UserStory changes, all its Flows may need to be updated.",
    "USES_FEATURE": "This Flow requires this Feature to complete its steps. If the Feature changes, this Flow's TestCases may be impacted.",
    "CALLS_API": "This Feature makes HTTP calls to this API endpoint. If the API schema changes (fields added/removed), this Feature and all Flows using it are potentially impacted.",
    "HAS_TEST_CASE": "This Flow is validated by this TestCase. When the Flow changes, this TestCase must be reviewed.",
    "DEPENDS_ON": "This Flow can only execute successfully after its dependency Flow has completed. A change in the dependency may cascade to this Flow."
  },
  "versioning": "Every node has a version number. is_current=true means the active version. is_current=false means a historical version that has been superseded. valid_at and invalid_at record exactly when each version was active. Expired nodes are never deleted -- they are the audit trail."
}
```

---

## Component 2 -- Context Builder

The Context Builder takes a question or entity reference and fetches the minimal relevant subgraph from Neo4j. It structures the subgraph as a readable document -- not raw Cypher results.

### Example: Impact query for a changed API

**Input:** "POST:/auth/login changed -- what is impacted?"

**Context Builder fetches (via Neo4j):**

```cypher
-- Fetch the changed API and its full upstream/downstream
MATCH path = (feat:Feature {is_current:true})-[:CALLS_API]->(ep:APIEndpoint {base_id:'POST:/auth/login', is_current:true})
MATCH (f:Flow {is_current:true})-[:USES_FEATURE]->(feat)
MATCH (us:UserStory {is_current:true})-[:HAS_FLOW]->(f)
OPTIONAL MATCH (f)-[:HAS_TEST_CASE]->(tc:TestCase {is_current:true})
RETURN us, f, feat, ep, collect(tc) AS test_cases
```

**Formats into LLM context:**

```
KNOWLEDGE GRAPH CONTEXT
========================

Changed Entity:
  APIEndpoint: POST:/auth/login (v2, active since 2026-05-24)
  Schema change: field 'password' removed, field 'otp' added

Impact path:
  POST:/auth/login
    <- CALLS_API <- Feature: Login (v1, active)
      <- USES_FEATURE <- Flow: f1 "User Login" (v3, active)
        <- HAS_FLOW <- UserStory: US1 "Plan Change" (v2, active)
        -> HAS_TEST_CASE -> TestCase: TC-f1-001 "Valid login with correct credentials" [positive, v1]
        -> HAS_TEST_CASE -> TestCase: TC-f1-002 "Login with wrong password" [negative, v1]
        -> DEPENDS_ON <- Flow: f2 "Fetch Plans" (v1, active)
          -> HAS_TEST_CASE -> TestCase: TC-f2-001 "Fetch plans successfully" [positive, v1]

Schema metadata:
  CALLS_API: Feature makes HTTP calls to this API. Schema change means Feature may need updating.
  USES_FEATURE: Flow uses this Feature. If Feature is impacted, Flow TestCases must be reviewed.
  HAS_TEST_CASE: Flow is validated by these TestCases. They must be reviewed after any upstream change.
```

**This structured context is what the LLM receives -- not raw graph data.**

---

## Component 3 -- Prompt Builder

The Prompt Builder assembles the final prompt sent to the LLM. It combines:
1. The schema context (static -- always included)
2. The subgraph context (dynamic -- fetched per query by Context Builder)
3. The user question

### Prompt template

```
You are a test engineering assistant with access to a knowledge graph that tracks
user stories, features, API endpoints, flows, and test cases for a software system.

SCHEMA CONTEXT:
{schema_context}

RELEVANT GRAPH DATA:
{subgraph_context}

QUESTION:
{user_question}

Instructions:
- Base your answer only on the graph data provided above
- Reference specific node IDs and version numbers in your answer
- If a TestCase needs to be regenerated, say so explicitly with its ID and reason
- If a node is missing from the graph, say so explicitly
- Do not make assumptions about data not present in the graph context
```

---

## Component 4 -- RAG Operations

The following operations are supported through the modeling layer:

### Operation 1: Impact query
**Question:** "What is impacted if [entity] changes?"  
**Context fetched:** The changed entity + all downstream nodes via `get_all_downstream()`  
**LLM task:** Classify each downstream node as: directly impacted / indirectly impacted / likely unaffected (and why)

### Operation 2: Coverage query
**Question:** "Does Flow [f1] have sufficient test coverage?"  
**Context fetched:** The flow + all its test cases + its features + APIs  
**LLM task:** Identify gaps -- missing negative tests, edge cases not covered, stale tests for changed APIs

### Operation 3: Change summary
**Question:** "What changed between v1 and v2 of UserStory US1?"  
**Context fetched:** Both versions of the node + word diff + downstream node diff  
**LLM task:** Write a human-readable summary of the change and its implications

### Operation 4: Validation query
**Question:** "Is the graph complete for story US1?"  
**Context fetched:** Full graph under US1  
**LLM task:** Check: all flows have features, all features have APIs, all flows have both positive and negative test cases, no missing nodes

### Operation 5: Regeneration trigger
**Question:** "Which test cases need to be regenerated after this API change?"  
**Context fetched:** Impact report for the changed API  
**LLM task:** Return a prioritized list of test cases to regenerate, with justification for each

---

## Implementation Plan

### File: `services/modeling_layer.py`

```python
class ModelingLayer:
    def __init__(self, graph_service, llm_client):
        self.gs = graph_service
        self.llm = llm_client
        self.schema_context = self._load_schema_context()

    def query(self, question: str, entity_type: str = None, base_id: str = None) -> str:
        """
        Main entry point. Given a question and optional entity context,
        fetch the relevant subgraph, build prompt, call LLM, return answer.
        """
        subgraph = self._build_context(entity_type, base_id)
        prompt = self._build_prompt(question, subgraph)
        return self.llm.complete(prompt)

    def _build_context(self, entity_type: str, base_id: str) -> str:
        """
        Fetch relevant subgraph from Neo4j and format as readable text.
        """
        # Fetch node + all downstream
        # Format as structured text (not raw Neo4j records)
        pass

    def _build_prompt(self, question: str, subgraph_context: str) -> str:
        """
        Assemble schema context + subgraph context + question into final prompt.
        """
        pass

    def _load_schema_context(self) -> str:
        """
        Load the static schema metadata that describes node/edge types.
        """
        pass
```

### LLM Client

```python
class LLMClient:
    def __init__(self, api_key: str, model: str = "claude-opus-4-7"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def complete(self, prompt: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
```

---

## What the LLM Does vs What Neo4j Does

| Responsibility | Neo4j | LLM (via Modeling Layer) |
|---|---|---|
| Store nodes and edges | YES | NO |
| Version history | YES | NO |
| Exact relationship traversal | YES | NO |
| Schema enforcement | YES | NO |
| Impact path detection | YES (graph traversal) | NO |
| Reasoning about *why* something is impacted | NO | YES |
| Summarising changes in plain English | NO | YES |
| Identifying test coverage gaps | NO | YES |
| Prioritising what to regenerate | NO | YES |
| Writing new test case content | NO | YES |

**Neo4j is the source of truth. The LLM is the reasoning engine. The modeling layer connects them.**
