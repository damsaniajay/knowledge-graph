# Versioning & temporal fields (doc vs implementation)

Reference: **`KnowledgeGraph_Schema (3).docx`** review comments.

## What the reviewer actually said

Comments anchored on **`valid_at`** / **`invalid_at`** rows (comment ids 3, 10) were from **Aravinda PR**:

> **"Status still is missing. Can that be brought in."**

On Feature / TestCase tables (comments 14, 25):

> **"Same comment as earlier on the status."**

So the ask was **add `status`**, not delete timeline columns. Damsani then added `status` on each entity.

## Why it feels redundant now

The doc ended up with **three** ways to say “is this row active?”:

| Mechanism | Meaning |
|-----------|---------|
| `is_current` | `true` = query this version in the live graph |
| `status` | `active` / `archived` (lifecycle — what review asked for) |
| `valid_from` / `valid_to` | **When** the version was active (timeline — doc history tables) |

Old code used **`valid_at` / `invalid_at`** (different names, same idea as `valid_from` / `valid_to`). That mismatch is what feels “off”.

You do **not** need both naming pairs. Pick one timeline pair + `status` + `is_current`.

## Canonical model (this repo)

| Field | Role |
|-------|------|
| `status` | Lifecycle: `active` or `archived` (reviewer requirement) |
| `is_current` | Fast filter for “live” node in Cypher/UI |
| `valid_from` | ISO time version became active |
| `valid_to` | ISO time version was superseded; `null` if still active |
| `created_at` / `created_by` / `updated_at` / `updated_by` | Audit (who/when edited) |

**Removed from new writes:** `valid_at`, `invalid_at` (legacy only; migrated on repair).

**Not the same as:** `VALIDATES_AGAINST` (edge type: TestCase → APIResponseSchema).

## SQL history tables (doc) vs Neo4j (app)

| Docx | Neo4j equivalent |
|------|------------------|
| `user_story` + `userstory_history` | All `UserStory` nodes; `is_current` + `valid_to` |
| `valid_from` / `valid_to` on history row | `valid_from` / `valid_to` on each version node |
| Separate `edge_history` table | Relationship `valid_from` / `valid_to` (when implemented fully) |

No PostgreSQL tables are created by this service — see **`docs/STORAGE.md`**.

## Repair existing Neo4j data

After upgrading code, run once:

```bash
python main.py setup-schema
# or POST /api/graph/repair-schema
```

This copies `valid_at` → `valid_from`, `invalid_at` → `valid_to` on old nodes and drops legacy property names where possible.
