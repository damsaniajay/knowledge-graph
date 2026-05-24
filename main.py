"""
main.py  —  Engineer 3
Aravinda Knowledge Graph Demo CLI

Commands:
  setup-schema
  upload-story    <file.json>
  upload-feature  <file.json>
  upload-api      <file.yaml>
  upload-flow     <file.json>
  upload-testcase <file.json>
  show-graph      <story_base_id>
  show-history    <entity_type> <base_id>
  delta           <story_base_id>
  neo4j-query     <story_base_id>

Design:
  Every upload → save node → relationship_mapper.map_on_upload() runs automatically
  New entity  → v1 node created, edges to matching existing nodes
  Re-upload   → v2 node created, old node expired (invalid_at set), new edges created
"""

import sys
import io
import json
import yaml

# Force UTF-8 output on Windows terminals
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from services import schema_service
from services import graph_service as gs
from services import relationship_mapper as mapper
from services import impact_analyser


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hr(char: str = "─", width: int = 60) -> str:
    return char * width


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _read_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_openapi(spec: dict) -> list[dict]:
    endpoints = []
    for path, path_item in spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            req_schema = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            endpoints.append({
                "path":           path,
                "method":         method.upper(),
                "summary":        operation.get("summary", ""),
                "request_schema": req_schema,
            })
    return endpoints


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

def cmd_setup_schema() -> None:
    print(f"\n{_hr()}")
    print("  SETUP SCHEMA")
    print(_hr())
    schema_service.setup()


def cmd_upload_story(file_path: str) -> None:
    data = _read_json(file_path)
    print(f"\n{_hr()}")
    print(f"  UPLOAD USER STORY")
    print(f"  File  : {file_path}")
    print(f"  ID    : {data['story_id']}  —  {data['title']}")
    print(_hr())

    result = gs.save_user_story(data)
    action = "INSERTED (v1)" if result["is_new"] else f"UPDATED  (v{result['version']})"
    print(f"\n  {action}  →  {result['node_id']}")

    print("\n  Running relationship mapper...")
    mapped = mapper.map_on_upload("user_story", data["story_id"])
    n = len(mapped["edges_created"])
    print(f"  {n} edge(s) created." if n else "  No matching nodes yet — node stands alone.")
    if not result["is_new"]:
        report = impact_analyser.analyse("user_story", data["story_id"])
        _print_impact_report(report)
    _print_neo4j_hint(data["story_id"])


def cmd_upload_feature(file_path: str) -> None:
    data = _read_json(file_path)
    print(f"\n{_hr()}")
    print(f"  UPLOAD FEATURE")
    print(f"  File    : {file_path}")
    print(f"  ID      : {data['feature_id']}  —  {data['name']}")
    print(f"  APIs    : {data.get('apis_used', [])}")
    print(_hr())

    result = gs.save_feature(data)
    action = "INSERTED (v1)" if result["is_new"] else f"UPDATED  (v{result['version']})"
    print(f"\n  {action}  →  {result['node_id']}")

    print("\n  Running relationship mapper...")
    mapped = mapper.map_on_upload("feature", data["feature_id"])
    n = len(mapped["edges_created"])
    print(f"  {n} edge(s) created." if n else "  No matching nodes yet — node stands alone.")
    if not result["is_new"]:
        report = impact_analyser.analyse("feature", data["feature_id"])
        _print_impact_report(report)
    _print_neo4j_hint()


def cmd_upload_api(file_path: str) -> None:
    spec      = _read_yaml(file_path)
    version   = spec.get("info", {}).get("version", "?")
    endpoints = _parse_openapi(spec)

    print(f"\n{_hr()}")
    print(f"  UPLOAD API SPEC  (v{version})")
    print(f"  File      : {file_path}")
    print(f"  Endpoints : {len(endpoints)}")
    print(_hr())

    for ep in endpoints:
        result = gs.save_endpoint(ep)
        action = "INSERTED" if result["is_new"] else f"UPDATED v{result['version']}"
        print(f"\n  {action:<12}  {ep['method']:6} {ep['path']}")
        print(f"               node_id: {result['node_id']}")
        mapped = mapper.map_on_upload("api_endpoint", result["base_id"])
        if mapped["edges_created"]:
            print(f"               {len(mapped['edges_created'])} edge(s) created.")
        if not result["is_new"]:
            report = impact_analyser.analyse("api_endpoint", result["base_id"])
            _print_impact_report(report)

    _print_neo4j_hint()


def cmd_upload_flow(file_path: str) -> None:
    data = _read_json(file_path)
    print(f"\n{_hr()}")
    print(f"  UPLOAD FLOW")
    print(f"  File          : {file_path}")
    print(f"  ID            : {data['flow_id']}  —  {data['title']}")
    print(f"  Story         : {data.get('story_id', '—')}")
    print(f"  Features used : {data.get('features_used', [])}")
    print(f"  Depends on    : {data.get('depends_on', [])}")
    print(_hr())

    result = gs.save_flow(data)
    action = "INSERTED (v1)" if result["is_new"] else f"UPDATED  (v{result['version']})"
    print(f"\n  {action}  →  {result['node_id']}")

    print("\n  Running relationship mapper...")
    mapped = mapper.map_on_upload("flow", data["flow_id"])
    n = len(mapped["edges_created"])
    print(f"  {n} edge(s) created." if n else "  No matching nodes yet — node stands alone.")
    if not result["is_new"]:
        report = impact_analyser.analyse("flow", data["flow_id"])
        _print_impact_report(report)
    _print_neo4j_hint(data.get("story_id"))


def cmd_upload_testcase(file_path: str) -> None:
    data = _read_json(file_path)
    print(f"\n{_hr()}")
    print(f"  UPLOAD TEST CASE")
    print(f"  File    : {file_path}")
    print(f"  ID      : {data['tc_id']}  —  {data['title']}")
    print(f"  Flow    : {data.get('flow_id', '—')}")
    print(f"  Type    : {data.get('type', '—')}")
    print(_hr())

    result = gs.save_test_case(data)
    action = "INSERTED (v1)" if result["is_new"] else f"UPDATED  (v{result['version']})"
    print(f"\n  {action}  →  {result['node_id']}")

    print("\n  Running relationship mapper...")
    mapped = mapper.map_on_upload("test_case", data["tc_id"])
    n = len(mapped["edges_created"])
    print(f"  {n} edge(s) created." if n else "  No matching nodes yet — node stands alone.")
    if not result["is_new"]:
        print("\n  [i] Test case updated. No downstream impact from test cases.")
    _print_neo4j_hint()


# ─────────────────────────────────────────────────────────────────────────────
# show-graph
# ─────────────────────────────────────────────────────────────────────────────

def cmd_show_graph(story_base_id: str) -> None:
    story = gs.get_user_story(story_base_id)
    if not story:
        print(f"\n  [!] UserStory '{story_base_id}' not found.")
        return

    print(f"\n{_hr('═')}")
    print(f"  KNOWLEDGE GRAPH  —  {story_base_id}")
    print(_hr('═'))
    print(f"\n  📖 UserStory : {story['base_id']}  \"{story['title']}\"")
    print(f"      version  : v{story['version']}")
    print(f"      valid_at : {story['valid_at'][:19]}")

    flows = gs.get_connected_flows(story["node_id"])
    if not flows:
        print("\n  └── (no flows linked yet)")
    else:
        for i, flow in enumerate(flows):
            last_flow = i == len(flows) - 1
            _print_flow_tree(flow, last_flow)

    print(f"\n{_hr('─')}")
    print(f"  Neo4j query:")
    print(f"  MATCH p=({{base_id:'{story_base_id}', is_current:true}})-[*1..6]->(n)")
    print(f"  WHERE coalesce(n.is_current, true) = true RETURN p")
    print(_hr('═'))


def _print_flow_tree(flow: dict, is_last: bool) -> None:
    branch = "└──" if is_last else "├──"
    cont   = "    " if is_last else "│   "

    deps = gs.get_depends_on(flow["node_id"])
    dep_str = f"  ← depends_on: {', '.join(d['base_id'] for d in deps)}" if deps else ""

    print(f"\n  │")
    print(f"  {branch} 🔄 Flow: {flow['base_id']}  \"{flow['title']}\"  "
          f"v{flow['version']}{dep_str}")

    features = gs.get_connected_features(flow["node_id"])
    tcs      = gs.get_connected_test_cases(flow["node_id"])

    for feat in features:
        print(f"  {cont}   ├── 🧩 Feature: {feat['name']}  v{feat['version']}")
        apis = gs.get_connected_apis(feat["node_id"])
        for api in apis:
            print(f"  {cont}   │       └── 🔌 {api['method']}  {api['path']}  v{api['version']}")

    for tc in tcs:
        print(f"  {cont}   └── 📋 TestCase: {tc['base_id']}  [{tc['type']}]  "
              f"\"{tc['title'][:45]}\"  v{tc['version']}")


# ─────────────────────────────────────────────────────────────────────────────
# show-history
# ─────────────────────────────────────────────────────────────────────────────

def cmd_show_history(entity_type: str, base_id: str) -> None:
    fetchers = {
        "story":    gs.get_user_story_history,
        "feature":  gs.get_feature_history,
        "api":      gs.get_endpoint_history,
        "flow":     gs.get_flow_history,
        "testcase": gs.get_test_case_history,
    }
    fn = fetchers.get(entity_type)
    if not fn:
        print(f"  [!] Unknown type. Use: story | feature | api | flow | testcase")
        return

    history = fn(base_id)
    print(f"\n{_hr()}")
    print(f"  HISTORY  —  {entity_type.upper()} : {base_id}")
    print(_hr())

    if not history:
        print("  No history found.")
        return

    for h in history:
        valid   = (h.get("valid_at")   or "")[:19]
        invalid = (h.get("invalid_at") or "now (current)")[:19]
        marker  = "  ◀ CURRENT" if not h.get("invalid_at") else ""
        print(f"  v{h['version']}  {h.get('status',''):<8}  "
              f"{valid}  →  {invalid}{marker}")

    print(_hr())


# ─────────────────────────────────────────────────────────────────────────────
# delta
# ─────────────────────────────────────────────────────────────────────────────

def cmd_delta(story_base_id: str) -> None:
    story = gs.get_user_story(story_base_id)
    if not story:
        print(f"\n  [!] UserStory '{story_base_id}' not found.")
        return

    history = gs.get_user_story_history(story_base_id)
    if len(history) < 2:
        print(f"\n  [i] Only v1 exists. Upload story_v2.json to see delta.")
        return

    prev   = history[-2]
    latest = history[-1]

    print(f"\n{_hr('═')}")
    print(f"  DELTA REPORT  —  {story_base_id}")
    print(_hr('═'))
    print(f"  Previous : {prev['node_id']}   valid: {(prev['valid_at'] or '')[:19]}"
          f"  →  {(prev['invalid_at'] or '')[:19]}")
    print(f"  Current  : {latest['node_id']}   valid: {(latest['valid_at'] or '')[:19]}  (active)")

    # Content diff
    print(f"\n  CONTENT CHANGES")
    print(f"  {_hr('-', 48)}")
    with gs._get_driver().session() as session:
        old_r = session.run(
            "MATCH (n:UserStory {node_id:$id}) RETURN n.content AS c",
            id=prev["node_id"],
        ).single()
        new_r = session.run(
            "MATCH (n:UserStory {node_id:$id}) RETURN n.content AS c",
            id=latest["node_id"],
        ).single()

    old_c = (old_r["c"] if old_r else "").lower().split()
    new_c = (new_r["c"] if new_r else "").lower().split()

    added   = set(new_c) - set(old_c)
    removed = set(old_c) - set(new_c)

    if not added and not removed:
        print("  Content identical.")
    else:
        if added:
            print(f"  + New terms : {', '.join(sorted(added)[:12])}")
        if removed:
            print(f"  - Removed   : {', '.join(sorted(removed)[:12])}")

    # Downstream impact
    print(f"\n  DOWNSTREAM NODES  (connected to current version — may be impacted)")
    print(f"  {_hr('-', 48)}")
    downstream = gs.get_all_downstream(latest["node_id"])

    if not downstream:
        print("  No downstream nodes linked yet.")
    else:
        by_type: dict[str, list] = {}
        for node in downstream:
            by_type.setdefault(node["type"], []).append(node)
        for ntype, nodes in sorted(by_type.items()):
            print(f"\n  {ntype}  ({len(nodes)})")
            for n in nodes:
                print(f"    • {n['node_id']:<30}  \"{n['label'][:40]}\"")

    print(f"\n  → Re-upload changed flows/testcases to update the graph.")
    print(_hr('═'))


# ─────────────────────────────────────────────────────────────────────────────
# compare  (before vs after)
# ─────────────────────────────────────────────────────────────────────────────

def cmd_compare(story_base_id: str) -> None:
    """
    Show side-by-side comparison of v(n-1) vs v(n) graph state.
    Prints Neo4j browser queries for both versions + terminal diff.
    """
    history = gs.get_user_story_history(story_base_id)
    if len(history) < 2:
        print(f"\n  [i] Only one version exists for '{story_base_id}'.")
        print(f"      Upload a new version first, then run compare.")
        return

    prev   = history[-2]
    latest = history[-1]

    prev_node_id   = prev["node_id"]
    latest_node_id = latest["node_id"]

    print(f"\n{_hr('═')}")
    print(f"  COMPARE  —  {story_base_id}  :  v{prev['version']} vs v{latest['version']}")
    print(_hr('═'))

    # ── Neo4j browser queries ──────────────────────────────────────────────
    print(f"""
  PASTE IN NEO4J BROWSER TO VISUALISE:

  -- BEFORE  (v{prev['version']}  valid: {(prev['valid_at'] or '')[:19]})
  MATCH p=(old:UserStory {{node_id:'{prev_node_id}'}})-[*1..6]->(n)
  RETURN p

  -- AFTER   (v{latest['version']}  valid: {(latest['valid_at'] or '')[:19]})
  MATCH p=(new:UserStory {{node_id:'{latest_node_id}'}})-[*1..6]->(n)
  RETURN p

  -- BOTH TOGETHER  (old nodes grey, new nodes coloured in Neo4j browser)
  MATCH p=(us:UserStory {{base_id:'{story_base_id}'}})-[*1..6]->(n)
  RETURN p

  -- EDGE TIMELINE  (shows valid_at / invalid_at on every relationship)
  MATCH (a)-[r]->(b)
  WHERE a.base_id = '{story_base_id}' OR b.base_id = '{story_base_id}'
  RETURN a.node_id, type(r), r.valid_at, r.invalid_at, b.node_id
  ORDER BY r.valid_at
""")

    # ── Terminal diff: downstream nodes in v(n-1) vs v(n) ─────────────────
    print(f"  {_hr('-', 56)}")
    print(f"  DOWNSTREAM NODE DIFF  (terminal)")
    print(f"  {_hr('-', 56)}")

    old_nodes = gs.get_all_downstream(prev_node_id)
    new_nodes = gs.get_all_downstream(latest_node_id)

    old_ids = {n["node_id"]: n for n in old_nodes}
    new_ids = {n["node_id"]: n for n in new_nodes}

    added   = {k: v for k, v in new_ids.items() if k not in old_ids}
    removed = {k: v for k, v in old_ids.items() if k not in new_ids}
    same    = {k: v for k, v in new_ids.items() if k in old_ids}

    if same:
        print(f"\n  UNCHANGED  ({len(same)} nodes):")
        for node in same.values():
            print(f"    =  [{node['type']:<12}]  {node['node_id']:<32}  \"{node['label'][:35]}\"")

    if added:
        print(f"\n  ADDED  ({len(added)} nodes — new in v{latest['version']}):")
        for node in added.values():
            print(f"    +  [{node['type']:<12}]  {node['node_id']:<32}  \"{node['label'][:35]}\"")

    if removed:
        print(f"\n  REMOVED  ({len(removed)} nodes — only in v{prev['version']}):")
        for node in removed.values():
            print(f"    -  [{node['type']:<12}]  {node['node_id']:<32}  \"{node['label'][:35]}\"")

    if not added and not removed:
        print(f"\n  Graph structure unchanged between v{prev['version']} and v{latest['version']}.")
        print(f"  Only the story content itself changed — check CONTENT CHANGES below.")

    # ── Story content diff ─────────────────────────────────────────────────
    print(f"\n  STORY CONTENT DIFF")
    print(f"  {_hr('-', 56)}")
    with gs._get_driver().session() as session:
        old_r = session.run(
            "MATCH (n:UserStory {node_id:$id}) RETURN n.content AS c",
            id=prev_node_id,
        ).single()
        new_r = session.run(
            "MATCH (n:UserStory {node_id:$id}) RETURN n.content AS c",
            id=latest_node_id,
        ).single()

    old_words = set((old_r["c"] if old_r else "").lower().split())
    new_words = set((new_r["c"] if new_r else "").lower().split())
    added_w   = sorted(new_words - old_words)
    removed_w = sorted(old_words - new_words)

    if added_w:
        print(f"  + Added   : {', '.join(added_w)}")
    if removed_w:
        print(f"  - Removed : {', '.join(removed_w)}")
    if not added_w and not removed_w:
        print(f"  Content identical.")

    print(f"\n  → Based on the story change, re-upload the affected flows")
    print(f"    and testcases to reflect the new version in the graph.")
    print(_hr('═'))


# ─────────────────────────────────────────────────────────────────────────────
# neo4j-query
# ─────────────────────────────────────────────────────────────────────────────

def cmd_neo4j_query(story_base_id: str) -> None:
    history = gs.get_user_story_history(story_base_id)
    versions = {h["version"]: h["node_id"] for h in history}

    print(f"\n{_hr()}")
    print("  NEO4J BROWSER QUERIES")
    print(_hr())

    for v, node_id in sorted(versions.items()):
        label = "CURRENT" if not history[v-1].get("invalid_at") else f"v{v} (expired)"
        print(f"\n  -- v{v} graph  [{label}]")
        print(f"  MATCH p=(us:UserStory {{node_id:'{node_id}'}})-[*1..6]->(n) RETURN p")

    print(f"\n  -- ALL versions together (old + new nodes visible)")
    print(f"  MATCH p=(us:UserStory {{base_id:'{story_base_id}'}})-[*1..6]->(n) RETURN p")

    print(f"\n  -- Edge timeline (valid_at / invalid_at on every relationship)")
    print(f"  MATCH (a)-[r]->(b)")
    print(f"  WHERE a.base_id = '{story_base_id}' OR b.base_id = '{story_base_id}'")
    print(f"  RETURN a.node_id, type(r), r.valid_at, r.invalid_at, b.node_id")
    print(f"  ORDER BY r.valid_at")
    print(_hr())


def _print_impact_report(report: dict | None) -> None:
    """Print structured impact report after a v2+ upload."""
    if not report:
        return

    etype = report["entity_type"].replace("_", " ").upper()
    bid   = report["base_id"]
    old_v = report["old_version"]
    new_v = report["new_version"]

    print(f"\n  {_hr('─', 58)}")
    print(f"  IMPACT REPORT  --  {etype}: {bid}  (v{old_v} --> v{new_v})")
    print(f"  {_hr('─', 58)}")

    # What changed
    changes = report.get("changes", {})
    has_change = any(v for v in changes.values())
    if has_change:
        print(f"\n  WHAT CHANGED:")
        for key, vals in changes.items():
            if vals:
                sign  = "+" if "added" in key else "-"
                label = key.replace("_", " ").title()
                items = ", ".join(str(x) for x in vals)
                print(f"    {sign}  {label}: {items}")
    if report.get("steps_changed"):
        print(f"    ~  Flow steps updated (review test case steps)")

    # Impacted features (for API changes)
    imp_feats = report.get("impacted_features", [])
    if imp_feats:
        print(f"\n  IMPACTED FEATURES  ({len(imp_feats)}) -- call this API:")
        for f in imp_feats:
            print(f"    !  {f['node_id']:<30}  {f.get('name', f['base_id'])}")

    # Impacted flows
    imp_flows = report.get("impacted_flows", [])
    if imp_flows:
        if report["entity_type"] == "flow":
            lbl = "depend on this flow"
        elif report["entity_type"] == "user_story":
            lbl = "belong to this story"
        else:
            lbl = "use this"
        print(f"\n  IMPACTED FLOWS  ({len(imp_flows)}) -- {lbl}:")
        for f in imp_flows:
            via = f"  [via {f.get('via_feature','')}]" if f.get("via_feature") else ""
            print(f"    !  {f['node_id']:<30}  \"{f.get('title','')[:38]}\"{via}")

    # Directly impacted test cases
    imp_tcs = report.get("impacted_test_cases", [])
    if imp_tcs:
        print(f"\n  IMPACTED TEST CASES  ({len(imp_tcs)}) -- need review / re-upload:")
        for tc in imp_tcs:
            via = f"  [via flow {tc.get('via_flow','')}]" if tc.get("via_flow") else ""
            print(f"    !  {tc['node_id']:<34}  \"{tc.get('title','')[:30]}\"{via}")

    # Indirectly impacted test cases
    indir = report.get("indirect_impacted_test_cases", [])
    if indir:
        print(f"\n  INDIRECTLY IMPACTED TEST CASES  ({len(indir)}) -- via dependent flows:")
        for tc in indir:
            print(f"    ?  {tc['node_id']:<34}  [via flow {tc.get('via_flow','')}]")

    # Missing nodes
    missing = report.get("missing_nodes", {})
    for category, names in missing.items():
        if names:
            print(f"\n  MISSING NODES -- {category} (referenced but not in graph yet):")
            for name in names:
                print(f"    +  {name}  -- upload to complete the graph")

    # Summary
    total_tcs = len(imp_tcs) + len(indir)
    if total_tcs:
        print(f"\n  ACTION: {total_tcs} test case(s) flagged -- review and re-upload them.")
    elif not has_change and not report.get("steps_changed"):
        print(f"\n  No structural graph changes -- only metadata updated.")
    print(f"  {_hr('─', 58)}")


def _print_neo4j_hint(story_id: str | None = None) -> None:
    print()
    if story_id:
        print(f"  Visualise: python main.py show-graph {story_id}")
    print(f"  Neo4j browser: MATCH p=(n)-[*1..4]->(m) RETURN p LIMIT 100")


# ─────────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────────

USAGE = """
Aravinda Knowledge Graph Demo
══════════════════════════════════════════════════════

SETUP
  python main.py setup-schema

UPLOAD  (any entity, any order, any time)
  python main.py upload-story    sample_data/stories/story_v1.json
  python main.py upload-feature  sample_data/features/feature_login.json
  python main.py upload-api      sample_data/api/spec_v1.yaml
  python main.py upload-flow     sample_data/flows/f1_login.json
  python main.py upload-testcase sample_data/testcases/TC-f1-001.json

QUERY
  python main.py show-graph    US1
  python main.py show-history  story    US1
  python main.py show-history  feature  Login
  python main.py show-history  flow     f1
  python main.py show-history  testcase TC-f1-001

DELTA  (after uploading a v2 of any entity)
  python main.py delta US1

NEO4J BROWSER QUERIES
  python main.py neo4j-query US1

COMPARE  (before vs after — visual diff for Neo4j browser + terminal)
  python main.py compare US1
"""


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        return

    cmd = args[0]

    match cmd:
        case "setup-schema":
            cmd_setup_schema()
        case "upload-story" if len(args) == 2:
            cmd_upload_story(args[1])
        case "upload-feature" if len(args) == 2:
            cmd_upload_feature(args[1])
        case "upload-api" if len(args) == 2:
            cmd_upload_api(args[1])
        case "upload-flow" if len(args) == 2:
            cmd_upload_flow(args[1])
        case "upload-testcase" if len(args) == 2:
            cmd_upload_testcase(args[1])
        case "show-graph" if len(args) == 2:
            cmd_show_graph(args[1])
        case "show-history" if len(args) == 3:
            cmd_show_history(args[1], args[2])
        case "delta" if len(args) == 2:
            cmd_delta(args[1])
        case "neo4j-query" if len(args) == 2:
            cmd_neo4j_query(args[1])
        case "compare" if len(args) == 2:
            cmd_compare(args[1])
        case _:
            print(f"  [!] Unknown command: {' '.join(args)}")
            print(USAGE)


if __name__ == "__main__":
    main()
