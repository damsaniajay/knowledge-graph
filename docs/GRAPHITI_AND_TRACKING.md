# Graphiti + PostgreSQL tracking

## Current architecture (default)

| Concern | Implementation |
|---------|----------------|
| Live graph | **Neo4j** (`NEO4J_URI`) |
| Exact duplicate | `content_hash` on nodes |
| “Is this v2 of US1?” | `entity_identity.py` (LLM + heuristics) → `delta_summary` |
| Flow delta | `flow_derivation.py` / flow proposals (`delta` mode) |
| Story text diff | `main.py delta` (CLI), `flow_matcher.py` |
| Audit / SQL history | **PostgreSQL** (optional, this doc) |

## PostgreSQL tracking (implemented)

Set in `.env`:

```env
DATABASE_URL=postgresql://user:pass@localhost:5432/knowledge_graph
TRACKING_ENABLED=true
```

Apply schema:

```bash
python main.py setup-tracking
# or POST /api/tracking/setup
```

### Tables

| Table | Purpose |
|-------|---------|
| `entity_history` | Doc-style history row per `node_id` (`valid_from` / `valid_to`, `status`, payload snapshot) |
| `upload_events` | Every API/CLI upload (filename, identity, `delta_summary`) |
| `delta_events` | Structured delta when LLM reports a version update |
| `edge_history` | Reserved for relationship timeline (populate when edge versioning is fully wired) |

Neo4j remains the **system of record** for traversal and linking. Postgres is the **audit trail** for compliance, reporting, and BI.

Query example:

```sql
SELECT version, valid_from, valid_to, delta_summary
FROM entity_history h
LEFT JOIN delta_events d ON d.base_id = h.base_id AND d.to_version = h.version
WHERE h.entity_type = 'UserStory' AND h.base_id = 'US1'
ORDER BY h.version;
```

API: `GET /api/tracking/health`, `GET /api/tracking/history/story/US1`

## Graphiti for delta (optional — not default)

[Graphiti](https://github.com/getzep/graphiti) is useful for **unstructured episodes** (JIRA text, Confluence) where entities are not already typed.

**Use a hybrid, not a replacement:**

| Layer | Tool |
|-------|------|
| Structured graph (`HAS_FEATURE`, `USES_API`, …) | Custom Neo4j + `linking_engine.py` |
| Identity / “same entity?” on upload | `entity_identity.py` (keep) |
| Semantic “what changed in prose?” | Optional Graphiti episode diff **or** LLM diff on top of hashes |
| Audit rows | PostgreSQL `delta_events` |

**Do not** run Graphiti as the only store: it cannot enforce your schema edge types.

### Enabling Graphiti later

1. `pip install graphiti-core` (separate from main `requirements.txt` today).
2. Set `USE_GRAPHITI_DELTA=true` and use the same Neo4j or a dedicated instance.
3. On story upload, after `save_user_story`, call `add_episode()` with story text; compare episodes for narrative delta hints.
4. Persist Graphiti’s summary into `delta_events.delta_detail` — still write versions to Neo4j + `entity_history`.

Spike script (unchanged): `store_story_graphiti.py`.

## Recommended path

1. **Now:** PostgreSQL tracking + existing LLM/hash delta (done).
2. **Next:** Persist flow proposals to Postgres instead of process memory.
3. **Later:** Graphiti only if you ingest raw documents without JSON structure.
