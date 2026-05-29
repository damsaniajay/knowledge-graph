# Demo upload order (with impacted test cases)

Restart the API after pulling changes, then **hard-refresh** the browser (Ctrl+Shift+R).

## Step 0 — Reset (optional clean demo)

Delete KG in the UI, or:

```bash
# only if you want a fresh Neo4j
curl -X DELETE "http://localhost:8000/api/graph?confirm=yes"
python main.py setup-schema
```

## Step 1 — Base bundle (v1)

Upload **`sample_data/plan_change_bundle.yaml`** via the UI (Upload type: **Bundle** or Auto).

Creates **US1 v1**: core plan-change flow + OTP payment features + 9 test cases (including mod-path TCs for later).

Select **US1 — Plan Change v1** in the Story dropdown. No impact panel yet (first version).

## Step 2 — Story add (v2)

Upload **`sample_data/stories/story_add.json`**

- Story dropdown should switch to **US1 v2**
- **Sidebar:** “Impacted test cases” (under Upload)
- **Graph banner:** flow change + same list
- **Magenta borders** on impacted TC nodes

`story_add.json` includes an explicit **`flows`** array so impact is deterministic (not LLM-guessed).

Expected impacted (typical):

| Type | Test cases | Why |
|------|------------|-----|
| Direct | `TC-planswitch-001`, `TC-payment-001` | Downstream of new **EligibilityCheck** step |
| Indirect | `TC-planfetch-001`, `TC-login-001`, … | Transitive via `DEPENDENCY` |

## Step 3 — Story mod (v3)

Upload **`sample_data/stories/story_mod.json`** (explicit **`flows`** — OTP path, no legacy Payment / EligibilityCheck).

Expected impacted (typical):

| Type | Test cases | Why |
|------|------------|-----|
| Direct | `TC-payment-001` | **Payment** removed from flow (legacy pay path) |
| Indirect | `TC-planswitch-001`, `TC-planfetch-001`, … | Transitive via `DEPENDENCY` |

OTP TCs (`TC-payment-initiate-001`, `TC-payment-verify-otp-001`, `TC-plan-activate-001`) match the new flow — not listed as impacted.

## Tips

1. Always pick the **story version** you just uploaded in the Story dropdown (v2 or v3), not “All nodes”.
2. If the list is empty, click **↻ Refresh** and re-select that story version.
3. Upload preview (before Continue) also shows predicted impacted TCs for story files.

## CLI equivalent (no UI)

```bash
python main.py setup-schema
# Bundle via API/UI only — or ingest pieces manually (see DEMO_COVERAGE.md)
python main.py upload-story sample_data/stories/story_add.json
python main.py upload-story sample_data/stories/story_mod.json
```

CLI prints an impact report in the terminal after each story v2+ upload.
