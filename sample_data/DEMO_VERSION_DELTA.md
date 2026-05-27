# Demo: addition, modification, and deletion on the graph

## Upload versioning (important)

| When | What happens |
|------|----------------|
| **New entity** | Creates v1 — no prompt |
| **Identical file** | Blocked as duplicate |
| **Same entity, content changed** | UI shows **Cancel** / **Continue** — new version saved automatically; history kept |
| **No prompt / default API** | **Replace** — updates the current node in place (no extra Feature v2 on every upload) |

Graph highlights (green / amber / red) appear when viewing a story version whose flows changed vs the previous version.

---

After a **user story** re-upload (Continue on the confirm dialog), the graph highlights Feature nodes when comparing versions:

| Visual | Meaning | CSS class |
|--------|---------|-----------|
| Green border | **Added** — new step in `flows[]` | `version-added` |
| Amber border | **Modified** — still in flow, story scope or Feature version changed | `version-modified` |
| Red dashed, faded | **Removed** — dropped from `flows[]` (node stays, inactive) | `version-removed` |

## One upload — all three highlights (v1 → v3)

Prerequisites: API spec, all features (including `feature_analytics.json`), then **story v1**.

1. Upload `sample_data/stories/story_v1.json` (deprecate if re-uploading same `US1`).
2. Upload `sample_data/stories/story_v3.json` as the next version of the same story.
3. Select the **current** story version in the dropdown.

Expected delta banner:

- **+ Added:** Analytics  
- **− Removed:** Payment  
- **◎ Modified:** Login, PlanSwitch (story text scope changed vs v1)

## Smaller demos (two cases each)

| Transition | Added | Modified | Removed |
|------------|-------|----------|---------|
| v2 → v1 | Payment | — | — |
| v1 → v2 | — | Login, PlanSwitch | Payment |

## Optional: modification from Feature re-upload

1. Graph on story v2 with Login in flows.  
2. Upload `feature_login_otp.json` (deprecate).  
3. Re-upload or refresh graph — Login shows **modified** when the Feature version changed after the previous story was archived.

Restart the server and use **↻ Refresh** after code changes.
