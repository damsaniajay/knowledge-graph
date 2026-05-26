# LLM-Derived Flows — Schema & Architecture (for review)

> **Superseded by `docs/SCHEMA.md` and `KnowledgeGraph_Schema (3).docx`:** flows live on **UserStory.flows[]**, not as Flow nodes. Keep this doc for LLM delta/approval workflow ideas only.

**Status:** Design for Aravinda review — implementation paused until approved.

## Aligned decisions

| # | Decision | Rule |
|---|----------|------|
| D1 | Flows are LLM-derived | Humans do **not** upload flow JSON files |
| D2 | **Delta-only** on change | Story/feature/API v2 updates **only** create or version **affected** flows — unchanged flows stay as-is |
| D3 | **Human approval** required | No Flow node is written to Neo4j until a human **approves** (or edits then approves) the LLM proposal |

---

## 1. What we upload vs what the system creates

| Entity | Source | Who provides it |
|--------|--------|-----------------|
| **UserStory** | File / pipeline | Team / product |
| **Feature** | File / pipeline | Team / product |
| **APIEndpoint** | OpenAPI YAML | Team / platform |
| **TestCase** | **v1:** manual JSON · **v2:** LLM + approval | Team / QA now; system later (see `SCHEMA_TEST_CASES.md`) |
| **Flow** | **LLM extraction → human approval** | System proposes; **human commits** |

---

## 2. End-to-end pipeline

```
[Upload UserStory / Feature / API]
              ↓
   [Modeling layer: schema + metadata + current graph snapshot]
              ↓
   [LLM Flow Extractor]
     • v1 story: full extract (all flows for story)
     • v2+ change: DELTA only (see §4)
              ↓
   [Validator] — negative rules, no invented features/APIs
              ↓
   [Proposal store]  status = pending_approval
              ↓
   [Human review UI]  approve | edit+approve | reject
              ↓
   [Commit to Neo4j]  only on approve → save_flow + relationship_mapper
              ↓
   [Impact analyser]  on committed deltas only
```

The LLM does **not** query Neo4j directly. It receives context from the modeling layer (schema, feature catalog, API catalog, **list of existing flow base_ids** for delta).

---

## 3. LLM output contract (proposal — not yet in graph)

```json
{
  "proposal_id": "prop-2026-05-25-001",
  "story_id": "US1",
  "change_type": "delta",
  "flows": [
    {
      "flow_id": "f3",
      "action": "update",
      "title": "Switch Plan",
      "description": "…",
      "steps": ["…"],
      "features_used": ["PlanSwitch"],
      "depends_on": ["f2"],
      "confidence": 0.88,
      "evidence": "Story v2 adds OTP before plan switch.",
      "delta_reason": "story_v2_content_changed"
    }
  ]
}
```

### Per-flow `action` (delta mode)

| action | Meaning | Neo4j effect (after approval) |
|--------|---------|-------------------------------|
| `unchanged` | Semantically same as current version | **No write** — keep current `flow_id` node |
| `create` | New flow for this story | `save_flow` v1 |
| `update` | Existing `base_id`, content changed | `save_flow` → new version (v2+), expire previous |
| `deprecate` | No longer in story scope | Mark current expired / flag for impact (no new version) |

LLM must return `unchanged` explicitly for flows that still apply — those are **not** re-versioned.

---

## 4. Delta-only extraction (D2)

### When to run

| Trigger | Extraction mode |
|---------|-----------------|
| First time (story + features + APIs present) | **Full** — propose all flows |
| UserStory v2 | **Delta** — compare v1 vs v2 content + existing flows |
| Feature v2 | **Delta** — only flows with that feature in `features_used` |
| API v2 / new endpoint | **Delta** — only flows whose steps reference that path |
| Feature/API v1 upload (no story change) | **Full** if no flows yet; else **no-op** |

### Delta algorithm (conceptual)

1. Load **current** flows for `story_id` from Neo4j (`is_current: true`).
2. Build LLM prompt with:
   - story v1 vs v2 diff (or feature/API change summary),
   - existing flow summaries (`base_id`, title, steps, features_used, depends_on),
   - instruction: *output only `create` | `update` | `deprecate`; mark others `unchanged` in a separate list or omit from `flows[]`*.
3. Validator ensures:
   - `update` / `deprecate` reference existing `base_id`,
   - `create` does not collide with existing `base_id`,
   - no full graph replace in one shot.
4. On approval → commit **only** items in `flows[]` with action ≠ `unchanged`.

### What must NOT happen

- Re-extracting and re-versioning all flows on every story typo.
- Silent overwrite of flows the LLM did not mention in the delta batch.

---

## 5. Human approval workflow (D3)

### Proposal lifecycle

```
pending_approval → approved → committed
                 → rejected (logged, no Neo4j write)
                 → edited → pending_approval (re-validate)
```

| State | In Neo4j? | Visible in UI |
|-------|-----------|---------------|
| `pending_approval` | No | Review queue — diff vs current graph |
| `approved` | No (until commit job runs) | Ready to commit |
| `committed` | Yes | Normal graph node |
| `rejected` | No | Audit log only |

### Human actions

| Action | Result |
|--------|--------|
| **Approve** | Commit proposal as-is → `save_flow` + mapper |
| **Edit + approve** | Human adjusts title/steps/features_used/depends_on → re-validate → commit |
| **Reject** | Proposal discarded; optional reason stored |
| **Approve subset** | Allowed — commit only selected flows in batch |

### Demo requirement (Thursday)

After LLM extract, **pause** and show approval screen before any new flow nodes appear in Neo4j Browser / graph UI.

---

## 6. Validation & negative testing

| ID | Scenario | Expected |
|----|----------|----------|
| F-N1 | LLM proposes unknown feature | Reject proposal; no commit |
| F-N2 | LLM `update` on non-existent `flow_id` | Reject |
| F-N3 | Invalid `depends_on` | Reject edge at commit |
| F-N4 | Commit without approval | **Blocked** by API |
| F-N5 | Manual `flow.json` upload | **Rejected** — flows only via extractor |
| F-N6 | Story v2 with one changed step | **Only** affected flow in delta; others unchanged |
| F-N7 | Human rejects proposal | Zero Neo4j changes |
| F-N8 | Human edits `features_used` then approves | Committed flow matches edit, not raw LLM |

---

## 7. Demo script (revised)

1. Upload user story v1  
2. Upload features + API spec  
3. **Extract flows (LLM)** — full proposal → `pending_approval`  
4. **Human approve** (show review UI)  
5. **Commit** — graph shows `HAS_FLOW`, `USES_FEATURE`, `CALLS_API`, `DEPENDS_ON`  
6. Upload test cases (linked to committed `flow_id`s)  
7. Upload user story **v2**  
8. **Extract flows (delta)** — show only `f3` update (example), not f1/f2/f4  
9. **Human approve** delta → commit → **impact report** on downstream TCs  

---

## 8. Modeling layer (for LLM / RAG)

Provides:

- Schema + validation rules  
- Story diff text (v1 → v2)  
- Catalogs: features, APIs, **existing flows**  
- Delta prompt template (“do not re-output unchanged flows”)  
- Proposal store (pre-Neo4j)  

Does **not** allow LLM to run Cypher or commit directly.

---

## 9. REST API (flow proposals)

Full contract: **`docs/API_FLOW_PROPOSALS.md`**

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/flow-proposals/extract` | LLM full/delta → `pending_approval` |
| `GET` | `/api/flow-proposals` | Review queue |
| `GET` | `/api/flow-proposals/{id}` | Detail + diff vs graph |
| `PATCH` | `/api/flow-proposals/{id}/flows/{flow_id}` | Human edit |
| `POST` | `/api/flow-proposals/{id}/approve` | Approve (whole or subset) |
| `POST` | `/api/flow-proposals/{id}/reject` | Reject |
| `POST` | `/api/flow-proposals/{id}/commit` | Write to Neo4j |

## 10. Test cases (both phases)

Full spec: **`docs/SCHEMA_TEST_CASES.md`**

- **v1 demo:** manual testcase JSON after flows committed  
- **v2:** LLM test proposals + human approval + delta (mirrors flows)

## 11. Implementation map (after schema approval)

| Component | Responsibility |
|-----------|----------------|
| `flow_extractor.py` | LLM call; full vs delta mode |
| `flow_proposal_store.py` | pending / approved / rejected |
| `flow_validator.py` | F-N1–F-N8 |
| `api/routers/flow_proposals.py` | See API doc |
| `test_proposal_*` (v2) | LLM TC + approval |
| Frontend | Flow approval queue; TC upload now, TC approval later |
| Deprecate | `POST /api/nodes/flows` manual create |

---

## 12. Gap vs current repo

| Today | Target |
|-------|--------|
| Manual flow upload (CLI + UI) | Extract + approve + commit |
| Story v2 re-upload re-links all flows | Delta extractor only touches changed flows |
| Immediate Neo4j write | Proposal → human → commit |

---

## 13. Summary for Google Space

> **Flows:** LLM-proposed (delta-only on change), human approval before Neo4j — `docs/SCHEMA_LLM_FLOW_EXTRACTION.md` + `docs/API_FLOW_PROPOSALS.md`.  
> **Test cases:** manual upload for Thursday demo; LLM + approval in phase 2 — `docs/SCHEMA_TEST_CASES.md`.
