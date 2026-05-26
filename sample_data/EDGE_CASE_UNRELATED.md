# Edge case: unrelated entities (standalone nodes)

These files belong to a **different product area** than the Airtel Plan Change demo. Use them to verify that the graph does **not** force spurious links to `US1`, `Login`, `/auth/login`, etc.

## Files

| File | Type | Purpose |
|------|------|---------|
| `stories/story_unrelated_invoice.json` | User story | Finance / invoice export (`/billing/...`) |
| `features/feature_unrelated_notifications.json` | Feature | Push notifications (`/notifications/...`) |
| `endpoints/endpoint_unrelated_support.json` | API endpoint | `GET /support/tickets` |

## Note on `FLOW_REQUIRE_APPROVAL=true`

If approval is required, the story is **saved immediately** but `flows[]` stay empty until you approve/commit a flow proposal. An LLM failure during proposal creation no longer fails the upload (fixed) — you still get the node in the inventory.

## Expected behaviour (after Plan Change demo is loaded)

Upload each file (UI auto-detect or CLI where supported):

```bash
python main.py upload-feature   sample_data/features/feature_unrelated_notifications.json
python main.py upload-story     sample_data/stories/story_unrelated_invoice.json
python main.py upload-endpoint  sample_data/endpoints/endpoint_unrelated_support.json
```

| Entity | Assigned ID (typical) | Standalone? | Why |
|--------|----------------------|-------------|-----|
| User story | **US2** (new title → new story) | Yes | `flows[]` empty or no overlap with Login/PlanFetch/…; no `HAS_FEATURE` to plan-change features |
| Feature | **PushNotifications** | Yes | `apis_used` paths not in plan-change OpenAPI; no `USES_API` to `POST:/auth/login`, etc. |
| Endpoint | **GET:/support/tickets** | Yes | Path not mentioned in any story content or feature `apis_used` |

### What you should see in the UI

- Full graph (**All nodes**) shows plan-change cluster **plus** isolated nodes (or small unrelated cluster).
- Story filter **US1** — unrelated story/features/endpoint stay visible but **dimmed** (not in US1 neighborhood).
- Story filter **US2** (invoice story) — only invoice story + any edges it gained (usually none to plan-change nodes).
- **Refresh / re-link** must not create `HAS_FEATURE` from US1/US2 to `PushNotifications` unless flows/content explicitly include it.

## Verify in Neo4j

```cypher
MATCH (n {base_id: 'PushNotifications', is_current: true})
OPTIONAL MATCH (n)-[r]-(m)
WHERE m.is_current = true
RETURN labels(n)[0], n.base_id, type(r), labels(m)[0], m.base_id;
```

```cypher
MATCH (n {base_id: 'GET:/support/tickets', is_current: true})
OPTIONAL MATCH (n)-[r]-(m)
WHERE m.is_current = true
RETURN type(r), m.base_id;
```

Expect **no rows** for relationships to plan-change entities unless you manually linked them.

## Negative test (optional)

Upload `story_unrelated_invoice.json` **before** any plan-change features — invoice story should still have empty `flows[]` and remain standalone after features are added later.
