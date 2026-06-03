# Sample data — Airtel Plan Change demo

Aligned with **`docs/SCHEMA.md`**, **`docs/STORAGE.md`**, and **KnowledgeGraph_Schema (3).docx**.

## Upload format (no IDs required)

| Entity | Required fields | ID assignment |
|--------|-----------------|---------------|
| User story | `title`, `content` | Auto `US1`, `US2`, … or **same ID** when LLM/heuristic detects a version of an existing story |
| Feature | `name` | Uses **name** as `base_id` (e.g. `Login`) |
| Test case | `title`, `linked_to` | Auto `TC-…` or match by title + link |
| OpenAPI | `paths` | Per endpoint `METHOD:path` |

**Versioning:** Upload `story_v2.json` after `story_v1.json` — same title → matched to **US1**, new node version, updated `flows[]`. **Payment** stays in the graph but is **not** in US1 v2 flows (standalone).

## Story flows demo

| File | Expected `flows[]` on US1 (LLM/heuristic) |
|------|---------------------------------------------|
| `story_v1.json` | Login → PlanFetch → PlanSwitch → **Payment** |
| `story_v2.json` | Login → PlanFetch → PlanSwitch (**Payment** not in journey) |

`story_v2` explicitly states payment is out of scope. Feature **Payment** remains uploaded separately and appears **standalone** (no `HAS_FEATURE` from US1 v2).

## Recommended upload order

```bash
python main.py setup-schema

python main.py upload-feature  sample_data/features/feature_login.json
python main.py upload-feature  sample_data/features/feature_planfetch.json
python main.py upload-feature  sample_data/features/feature_planswitch.json
python main.py upload-feature  sample_data/features/feature_payment.json

python main.py upload-story    sample_data/stories/story_v1.json
# → US1 assigned; flows include Payment

python main.py upload-api      sample_data/api/spec_v1.yaml

python main.py upload-testcase sample_data/testcases/TC-US1-001.json
# … other testcases …

python main.py upload-story    sample_data/stories/story_v2.json
# → US1 v2; flows without Payment; Payment node standalone

python main.py show-graph US1
```

Web UI: http://localhost:9000 — same files, auto-detect type. **↻ Refresh** re-links from Neo4j.

## Environment

- `OPENAI_API_KEY` — flow derivation + entity identity matching (delta → same story id)
- `USE_LLM_ENTITY_MATCH=true` (default on when LLM flows are on)

See **`DEMO_COVERAGE.md`** for the edge matrix.

## Unrelated entities (standalone edge case)

After the plan-change demo, upload files in **`EDGE_CASE_UNRELATED.md`** to confirm unrelated story, feature, and endpoint stay **disconnected** from `US1` / Login / plan APIs.
