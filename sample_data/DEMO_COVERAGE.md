# Demo coverage matrix

Every **required edge** from the schema, which sample produces it, and what to check in Neo4j / UI.

**Storage:** all nodes and edges live in **Neo4j** (see `docs/STORAGE.md`). Files here are upload inputs only.

## Lean bundle (recommended)

One upload builds a small but complete graph:

| Asset | Count | Notes |
|-------|-------|--------|
| APIs | 10 | Core plan change + eligibility + mod-payment paths + support |
| Features | 9 | Login → … → Payment; EligibilityCheck; SupportTicket; PaymentInitiate/VerifyOtp/PlanActivate (no TCs) |
| Test cases | 6 | Story/feature chains + one API-level negative + unrelated support |

```bash
python main.py setup-schema
python main.py upload-bundle sample_data/plan_change_bundle.yaml
python main.py show-graph US1
```

Then optional deltas:

```bash
python main.py upload-story sample_data/stories/story_add.json   # adds EligibilityCheck to flow
python main.py upload-story sample_data/stories/story_mod.json   # extends flow with OTP payment steps
```

## Relationship checklist

| Edge | From → To | How the demo creates it | Sample |
|------|-----------|-------------------------|--------|
| **HAS_FEATURE** | UserStory → Feature | `flows[]` on story (LLM or bundle story) | Bundle `user_story.flows` |
| **USES_API** | UserStory / Feature → APIEndpoint | Paths in story content / `apis_used` | `plan_change_bundle.yaml` |
| **HAS_TEST_CASE** | Story / Feature / API → TestCase | `linked_to` + bundle `test_cases` | 6 TCs in bundle |
| **DEPENDS_ON** | Feature → Feature | `flows[]` order + `depends_on` on features | Bundle features |
| **DEPENDS_ON** | TestCase → TestCase | `depends_on_test_cases` | e.g. TC-planfetch-001 → TC-login-001 |
| **DEPENDENCY** | TestCase → TestCase | Mirror of TC DEPENDS_ON (impact queries) | Auto on upload / relink |
| **HAS_RESPONSE_SCHEMA** | APIEndpoint → APIResponseSchema | OpenAPI `responses` | Bundle `openapi` section |

**Flows:** property `UserStory.flows` on node `US1` — not a graph edge.

## Corner cases in the lean bundle

| Scenario | Where | Expected |
|----------|--------|----------|
| Feature + API TC chains | `depends_on_test_cases` on TC-planfetch, TC-planswitch, TC-payment | DEPENDS_ON + DEPENDENCY between TCs |
| API-level negative | `TC-API-login-401` | `POST:/auth/login -[HAS_TEST_CASE]->` |
| Unrelated cluster | SupportTicket feature + TC-support-001 | Edges to Login only, not plan-change chain |
| story_add | `story_add.json` | EligibilityCheck in flow (API already in bundle) |
| story_mod | `story_mod.json` | PaymentInitiate → … → PlanActivate in flow (features/APIs in bundle, no mod TCs) |
| Versioning | `story_v2.json` | Same US1 base_id, fewer features in flows |

## Verify in Neo4j Browser

```cypher
MATCH (us:UserStory {base_id:'US1', is_current:true})
OPTIONAL MATCH (us)-[r]->(n)
WHERE n.is_current = true
RETURN type(r) AS edge, labels(n)[0] AS target, n.base_id AS id
ORDER BY edge, id;
```

```cypher
MATCH (tc:TestCase {base_id:'TC-planfetch-001', is_current:true})-[:DEPENDS_ON]->(p)
RETURN tc.base_id, p.base_id;
```

## Granular upload (optional)

Use individual JSON/YAML files instead of the bundle — see `features/`, `stories/`, `testcases/`, `api/spec_v1.yaml`. The bundle is equivalent to uploading those pieces in one step with fewer nodes than the old 27-TC demo set.
