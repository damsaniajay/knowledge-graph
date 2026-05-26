# Demo coverage matrix

Every **required edge** from the schema, which sample file produces it, and what to check in Neo4j / UI.

**Storage:** all nodes and edges live in **Neo4j** (see `docs/STORAGE.md`). Files here are upload inputs only.

## Relationship checklist

| Edge | From → To | How the demo creates it | Sample file(s) |
|------|-----------|-------------------------|----------------|
| **HAS_FEATURE** | UserStory → Feature | Names in **LLM-derived** `flows[]` on story node | Upload **features first**, then `story_v1.json` (v2 omits Payment from flows) |
| **USES_API** | UserStory → APIEndpoint | API **path** appears in `story.content` | `story_v1.json` (mentions `/auth/login`, `/plans`, …) |
| **USES_API** | Feature → APIEndpoint | Path in `feature.apis_used[]` | `features/*.json` + `api/spec_v1.yaml` |
| **HAS_TEST_CASE** | UserStory → TestCase | `linked_to: "Plan Change"` (resolves to US1) | `testcases/TC-US1-001.json` |
| **HAS_TEST_CASE** | Feature → TestCase | `linked_to: "Login"` etc. | `testcases/TC-login-*.json`, … |
| **HAS_TEST_CASE** | APIEndpoint → TestCase | `linked_to: "POST:/auth/login"` | `testcases/TC-API-login-401.json` |
| **NEXT_STEP** | Feature → Feature | Order in LLM-derived `flows[]` | After story upload (auto) |
| **DEPENDS_ON** | Feature → Feature | `depends_on` on feature JSON | `feature_planfetch` → Login, etc. |
| **HAS_RESPONSE_SCHEMA** | APIEndpoint → APIResponseSchema | OpenAPI `responses` | `api/spec_v1.yaml` |
| **VALIDATES_AGAINST** | TestCase → APIResponseSchema | `type: negative` + link to feature/API with 4xx schema | `TC-login-002`, `TC-API-login-401`, … |

**Flows:** not an edge — stored as property `UserStory.flows` on node `US1`.

## Corner cases included

| Scenario | Files | Expected behaviour |
|----------|--------|-------------------|
| Story-level test | `TC-US1-001.json` | `US1 -[HAS_TEST_CASE]->` TC |
| API-level test | `TC-API-login-401.json` | `POST:/auth/login -[HAS_TEST_CASE]->` TC |
| Feature pos + neg | `TC-login-001/002` | Coverage per feature |
| Parameterized API | `feature_planfetch` + `/plans` | `USES_API` with `params=type=current` (linking engine) |
| Multi-step journey | LLM `flows[]` on UserStory node | v1: 4× `HAS_FEATURE` + 3× `NEXT_STEP`; v2: 3× `HAS_FEATURE` |
| Payment standalone | `story_v2.json` after v1 + Payment feature | Payment has no `HAS_FEATURE` from US1 v2 |
| Story mentions many APIs | `story_v1` content | Multiple `US1 -[USES_API]->` endpoints |
| Versioning | `story_v2.json` (no id in file) | Same **US1** base_id, new version; LLM/title match |
| Auto IDs | JSON without `story_id` / `feature_id` / `tc_id` | `services/entity_identity.py` |
| Out-of-order upload | any order | Re-link on Refresh (`POST /api/graph/relink`) |
| **Unrelated / standalone** | `story_unrelated_invoice.json`, `feature_unrelated_notifications.json`, `endpoint_unrelated_support.json` | No edges to plan-change cluster — see **`EDGE_CASE_UNRELATED.md`** |

## Verify in Neo4j Browser

```cypher
MATCH (us:UserStory {base_id:'US1', is_current:true})
OPTIONAL MATCH (us)-[r]->(n)
WHERE n.is_current = true
RETURN type(r) AS edge, labels(n)[0] AS target, n.base_id AS id
ORDER BY edge, id;
```

```cypher
MATCH (us:UserStory {base_id:'US1', is_current:true})
RETURN us.flows AS flows_on_story_node;
```

## Full upload script

```bash
python main.py setup-schema

python main.py upload-feature  sample_data/features/feature_login.json
python main.py upload-feature  sample_data/features/feature_planfetch.json
python main.py upload-feature  sample_data/features/feature_planswitch.json
python main.py upload-feature  sample_data/features/feature_payment.json

python main.py upload-story    sample_data/stories/story_v1.json

python main.py upload-api      sample_data/api/spec_v1.yaml

python main.py upload-testcase sample_data/testcases/TC-US1-001.json
python main.py upload-testcase sample_data/testcases/TC-login-001.json
python main.py upload-testcase sample_data/testcases/TC-login-002.json
python main.py upload-testcase sample_data/testcases/TC-API-login-401.json
python main.py upload-testcase sample_data/testcases/TC-planfetch-001.json
python main.py upload-testcase sample_data/testcases/TC-planfetch-002.json
python main.py upload-testcase sample_data/testcases/TC-planswitch-001.json
python main.py upload-testcase sample_data/testcases/TC-planswitch-002.json
python main.py upload-testcase sample_data/testcases/TC-payment-001.json
python main.py upload-testcase sample_data/testcases/TC-payment-002.json

python main.py show-graph US1
```
