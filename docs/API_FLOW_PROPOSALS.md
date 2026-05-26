# Flow Proposal API — REST contract

Base path: `/api/flow-proposals`

All flow writes to Neo4j go through this API after **human approval**. Manual `POST /api/nodes/flows` is deprecated (returns `410` when `FLOW_MANUAL_UPLOAD=false`).

---

## Lifecycle

```
POST /extract  →  pending_approval
POST /{id}/approve  →  approved
POST /{id}/reject   →  rejected
POST /{id}/commit    →  committed (Neo4j)
```

Optional: `PATCH /{id}/flows/{flow_id}` to edit a proposed flow before approve.

---

## Endpoints

### `POST /api/flow-proposals/extract`

Trigger LLM extraction (full or delta).

**Query**

| Param | Type | Description |
|-------|------|-------------|
| `story_id` | string | Required. User story `base_id` |
| `mode` | string | `full` (default if no flows) or `delta` |
| `trigger` | string | `story_v2` \| `feature_v2` \| `api_v2` \| `manual` |

**Body (optional)**

```json
{
  "feature_id": "Login",
  "api_base_id": "POST:/plans/switch"
}
```

Used for delta scoping when `trigger` is feature/API change.

**Response `201`**

```json
{
  "proposal_id": "prop-a1b2c3",
  "story_id": "US1",
  "mode": "delta",
  "status": "pending_approval",
  "flows": [
    {
      "flow_id": "f3",
      "action": "update",
      "title": "Switch Plan",
      "description": "...",
      "steps": ["..."],
      "features_used": ["PlanSwitch"],
      "depends_on": ["f2"],
      "confidence": 0.88,
      "evidence": "Story v2 adds OTP.",
      "delta_reason": "story_v2_content_changed"
    }
  ],
  "unchanged_flow_ids": ["f1", "f2", "f4"],
  "validation": { "passed": true, "errors": [] },
  "created_at": "2026-05-25T10:00:00Z"
}
```

**Errors**

| Code | When |
|------|------|
| `400` | Missing story, no features/APIs for full extract |
| `404` | Story not found |
| `422` | Validator failed — proposal not created |

---

### `GET /api/flow-proposals`

List proposals (review queue).

**Query:** `story_id`, `status` (`pending_approval` \| `approved` \| `rejected` \| `committed`)

**Response `200`**

```json
{
  "proposals": [
    {
      "proposal_id": "prop-a1b2c3",
      "story_id": "US1",
      "mode": "delta",
      "status": "pending_approval",
      "flow_count": 1,
      "created_at": "..."
    }
  ]
}
```

---

### `GET /api/flow-proposals/{proposal_id}`

Full proposal + diff vs current Neo4j flows.

**Response `200`**

```json
{
  "proposal_id": "prop-a1b2c3",
  "story_id": "US1",
  "status": "pending_approval",
  "flows": [ "... same as extract ..." ],
  "unchanged_flow_ids": ["f1", "f2", "f4"],
  "diff": [
    {
      "flow_id": "f3",
      "action": "update",
      "current_version": 1,
      "fields_changed": ["steps", "description"]
    }
  ]
}
```

---

### `PATCH /api/flow-proposals/{proposal_id}/flows/{flow_id}`

Human edit before approval.

**Body:** subset of flow fields (`title`, `description`, `steps`, `features_used`, `depends_on`)

**Response `200`:** updated flow entry + `edited_by_human: true`

---

### `POST /api/flow-proposals/{proposal_id}/approve`

**Body (optional)**

```json
{
  "flow_ids": ["f3"],
  "comment": "Looks good"
}
```

Omit `flow_ids` to approve entire batch.

**Response `200`**

```json
{
  "proposal_id": "prop-a1b2c3",
  "status": "approved",
  "approved_flow_ids": ["f3"]
}
```

---

### `POST /api/flow-proposals/{proposal_id}/reject`

**Body**

```json
{
  "reason": "Steps do not match payment flow"
}
```

**Response `200`:** `{ "status": "rejected" }` — no Neo4j changes.

---

### `POST /api/flow-proposals/{proposal_id}/commit`

Writes **approved** flows only to Neo4j.

**Precondition:** `status === approved`

**Response `200`**

```json
{
  "proposal_id": "prop-a1b2c3",
  "status": "committed",
  "committed": [
    { "flow_id": "f3", "node_id": "f3_v2", "version": 2, "action": "update" }
  ],
  "edges_created": [["US1_v1", "HAS_FLOW", "f3_v2"]],
  "graph": { "nodes": [], "edges": [] }
}
```

**Errors**

| Code | When |
|------|------|
| `409` | Not approved yet |
| `422` | Re-validation failed at commit |

---

## Deprecations

| Old | New |
|-----|-----|
| `POST /api/nodes/flows` | `POST /api/flow-proposals/extract` → approve → commit |
| `POST /api/upload` (flow json) | Reject flow files; return hint to use extract |

---

## Frontend (approval UI)

| Screen | API calls |
|--------|-----------|
| After story/features/API upload | `POST /extract` |
| Review queue | `GET /flow-proposals?status=pending_approval` |
| Diff view | `GET /flow-proposals/{id}` |
| Edit step | `PATCH .../flows/{flow_id}` |
| Approve / Reject | `POST .../approve` or `reject` |
| Show graph | `POST .../commit` → use `graph` in response |
