"""
graph_service.py  —  Engineer 1
Neo4j CRUD for all 5 entity types in Aravinda's Knowledge Graph demo.

Graph model:
  (:UserStory)-[:HAS_FLOW]->(:Flow)-[:USES_FEATURE]->(:Feature)-[:CALLS_API]->(:APIEndpoint)
  (:Flow)-[:HAS_TEST_CASE]->(:TestCase)
  (:Flow)-[:DEPENDS_ON]->(:Flow)

Every node is versioned:
  base_id    — stable identifier  e.g. "US1", "Login", "f1", "TC-f1-001"
  node_id    — versioned id       e.g. "US1_v1", "Login_v2"
  version    — int
  is_current — bool  (only one version active at a time)
  valid_at   — ISO timestamp when this version became active
  invalid_at — ISO timestamp when superseded  (null = still active)

Every edge carries:
  valid_at, invalid_at
"""

import json
import logging
from datetime import datetime, timezone
from neo4j import GraphDatabase
import config

logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

_driver = None

VALID_REL_TYPES = {
    "HAS_FLOW", "USES_FEATURE", "CALLS_API",
    "HAS_TEST_CASE", "DEPENDS_ON",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _vid(base_id: str, version: int) -> str:
    """Build a versioned node_id."""
    return f"{base_id}_v{version}"


def _get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
        )
    return _driver


def _current_version(session, label: str, base_id: str) -> int:
    """Return the current version number for a node, 0 if it doesn't exist yet."""
    result = session.run(
        f"MATCH (n:{label} {{base_id: $base_id, is_current: true}}) "
        "RETURN n.version AS v",
        base_id=base_id,
    )
    record = result.single()
    return int(record["v"]) if record else 0


def _expire_current(session, label: str, base_id: str, now: str) -> None:
    """Mark the current version of a node as expired."""
    session.run(
        f"MATCH (n:{label} {{base_id: $base_id, is_current: true}}) "
        "SET n.is_current = false, n.invalid_at = $now, n.status = 'expired'",
        base_id=base_id,
        now=now,
    )


# ─────────────────────────────────────────────────────────────────────────────
# UserStory
# ─────────────────────────────────────────────────────────────────────────────

def save_user_story(story: dict, created_by: str = "system") -> dict:
    """
    Insert or update a UserStory node.
    Returns {node_id, version, is_new}.
    """
    base_id = story["story_id"]
    now = _now()

    with _get_driver().session() as session:
        cur = _current_version(session, "UserStory", base_id)
        new_v = cur + 1
        node_id = _vid(base_id, new_v)
        is_new = cur == 0

        if not is_new:
            _expire_current(session, "UserStory", base_id, now)

        session.run(
            """
            CREATE (n:UserStory {
                node_id:    $node_id,
                base_id:    $base_id,
                title:      $title,
                content:    $content,
                version:    $version,
                status:     'active',
                is_current: true,
                valid_at:   $now,
                invalid_at: null,
                created_at: $now,
                created_by: $created_by
            })
            """,
            node_id=node_id, base_id=base_id,
            title=story["title"],
            content=story.get("content", ""),
            version=new_v, now=now, created_by=created_by,
        )

    return {"node_id": node_id, "version": new_v, "is_new": is_new}


def get_user_story(base_id: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:UserStory {base_id:$b, is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, "
            "n.title AS title, n.content AS content, "
            "n.version AS version, n.valid_at AS valid_at",
            b=base_id,
        ).single()
        return dict(r) if r else None


def get_all_user_stories() -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:UserStory {is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, "
            "n.title AS title, n.version AS version, n.valid_at AS valid_at"
        )]


def get_user_story_history(base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:UserStory {base_id:$b}) "
            "RETURN n.node_id AS node_id, n.version AS version, "
            "n.status AS status, n.valid_at AS valid_at, n.invalid_at AS invalid_at "
            "ORDER BY n.version",
            b=base_id,
        )]


# ─────────────────────────────────────────────────────────────────────────────
# Feature
# ─────────────────────────────────────────────────────────────────────────────

def save_feature(feature: dict, created_by: str = "system") -> dict:
    base_id = feature["feature_id"]
    now = _now()

    with _get_driver().session() as session:
        cur = _current_version(session, "Feature", base_id)
        new_v = cur + 1
        node_id = _vid(base_id, new_v)
        is_new = cur == 0

        if not is_new:
            _expire_current(session, "Feature", base_id, now)

        session.run(
            """
            CREATE (n:Feature {
                node_id:     $node_id,
                base_id:     $base_id,
                name:        $name,
                description: $description,
                apis_used:   $apis_used,
                version:     $version,
                status:      'active',
                is_current:  true,
                valid_at:    $now,
                invalid_at:  null,
                created_at:  $now,
                created_by:  $created_by
            })
            """,
            node_id=node_id, base_id=base_id,
            name=feature["name"],
            description=feature.get("description", ""),
            apis_used=feature.get("apis_used", []),
            version=new_v, now=now, created_by=created_by,
        )

    return {"node_id": node_id, "version": new_v, "is_new": is_new}


def get_feature(base_id: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:Feature {base_id:$b, is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, "
            "n.name AS name, n.description AS description, "
            "n.apis_used AS apis_used, n.version AS version, n.valid_at AS valid_at",
            b=base_id,
        ).single()
        return dict(r) if r else None


def get_feature_by_name(name: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:Feature {name:$name, is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, "
            "n.name AS name, n.apis_used AS apis_used, n.version AS version",
            name=name,
        ).single()
        return dict(r) if r else None


def get_all_features() -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:Feature {is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, "
            "n.name AS name, n.apis_used AS apis_used, "
            "n.version AS version, n.valid_at AS valid_at"
        )]


def get_feature_history(base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:Feature {base_id:$b}) "
            "RETURN n.node_id AS node_id, n.version AS version, "
            "n.status AS status, n.valid_at AS valid_at, n.invalid_at AS invalid_at "
            "ORDER BY n.version",
            b=base_id,
        )]


# ─────────────────────────────────────────────────────────────────────────────
# APIEndpoint
# ─────────────────────────────────────────────────────────────────────────────

def save_endpoint(endpoint: dict, created_by: str = "system") -> dict:
    base_id = f"{endpoint['method'].upper()}:{endpoint['path']}"
    now = _now()

    with _get_driver().session() as session:
        cur = _current_version(session, "APIEndpoint", base_id)
        new_v = cur + 1
        node_id = _vid(base_id, new_v)
        is_new = cur == 0

        if not is_new:
            _expire_current(session, "APIEndpoint", base_id, now)

        session.run(
            """
            CREATE (n:APIEndpoint {
                node_id:         $node_id,
                base_id:         $base_id,
                path:            $path,
                method:          $method,
                summary:         $summary,
                request_schema:  $request_schema,
                response_schema: $response_schema,
                version:         $version,
                status:          'active',
                is_current:      true,
                valid_at:        $now,
                invalid_at:      null,
                created_at:      $now,
                created_by:      $created_by
            })
            """,
            node_id=node_id, base_id=base_id,
            path=endpoint["path"],
            method=endpoint["method"].upper(),
            summary=endpoint.get("summary", ""),
            request_schema=json.dumps(endpoint.get("request_schema", {})),
            response_schema=json.dumps(endpoint.get("response_schema", {})),
            version=new_v, now=now, created_by=created_by,
        )

    return {"node_id": node_id, "base_id": base_id, "version": new_v, "is_new": is_new}


def get_endpoint_by_path(path: str, method: str | None = None) -> dict | None:
    base_id = f"{method.upper()}:{path}" if method else None
    with _get_driver().session() as session:
        if base_id:
            r = session.run(
                "MATCH (n:APIEndpoint {base_id:$b, is_current:true}) "
                "RETURN n.node_id AS node_id, n.base_id AS base_id, "
                "n.path AS path, n.method AS method, "
                "n.summary AS summary, n.version AS version",
                b=base_id,
            ).single()
        else:
            r = session.run(
                "MATCH (n:APIEndpoint {path:$path, is_current:true}) "
                "RETURN n.node_id AS node_id, n.base_id AS base_id, "
                "n.path AS path, n.method AS method, "
                "n.summary AS summary, n.version AS version LIMIT 1",
                path=path,
            ).single()
        return dict(r) if r else None


def get_all_endpoints() -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:APIEndpoint {is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, "
            "n.path AS path, n.method AS method, "
            "n.summary AS summary, n.version AS version, n.valid_at AS valid_at"
        )]


def get_endpoint_history(base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:APIEndpoint {base_id:$b}) "
            "RETURN n.node_id AS node_id, n.version AS version, "
            "n.status AS status, n.valid_at AS valid_at, n.invalid_at AS invalid_at "
            "ORDER BY n.version",
            b=base_id,
        )]


# ─────────────────────────────────────────────────────────────────────────────
# Flow
# ─────────────────────────────────────────────────────────────────────────────

def save_flow(flow: dict, created_by: str = "system") -> dict:
    base_id = flow["flow_id"]
    now = _now()

    with _get_driver().session() as session:
        cur = _current_version(session, "Flow", base_id)
        new_v = cur + 1
        node_id = _vid(base_id, new_v)
        is_new = cur == 0

        if not is_new:
            _expire_current(session, "Flow", base_id, now)

        session.run(
            """
            CREATE (n:Flow {
                node_id:       $node_id,
                base_id:       $base_id,
                story_id:      $story_id,
                title:         $title,
                description:   $description,
                steps:         $steps,
                features_used: $features_used,
                depends_on:    $depends_on,
                version:       $version,
                status:        'active',
                is_current:    true,
                valid_at:      $now,
                invalid_at:    null,
                created_at:    $now,
                created_by:    $created_by
            })
            """,
            node_id=node_id, base_id=base_id,
            story_id=flow.get("story_id", ""),
            title=flow["title"],
            description=flow.get("description", ""),
            steps=json.dumps(flow.get("steps", [])),
            features_used=flow.get("features_used", []),
            depends_on=flow.get("depends_on", []),
            version=new_v, now=now, created_by=created_by,
        )

    return {"node_id": node_id, "version": new_v, "is_new": is_new}


def get_flow(base_id: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:Flow {base_id:$b, is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, "
            "n.story_id AS story_id, n.title AS title, "
            "n.steps AS steps, n.features_used AS features_used, "
            "n.depends_on AS depends_on, n.version AS version, n.valid_at AS valid_at",
            b=base_id,
        ).single()
        if not r:
            return None
        row = dict(r)
        if isinstance(row.get("steps"), str):
            row["steps"] = json.loads(row["steps"])
        return row


def get_all_flows() -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:Flow {is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, "
            "n.story_id AS story_id, n.title AS title, "
            "n.features_used AS features_used, n.depends_on AS depends_on, "
            "n.version AS version, n.valid_at AS valid_at"
        )]


def get_flow_history(base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:Flow {base_id:$b}) "
            "RETURN n.node_id AS node_id, n.version AS version, "
            "n.status AS status, n.valid_at AS valid_at, n.invalid_at AS invalid_at "
            "ORDER BY n.version",
            b=base_id,
        )]


# ─────────────────────────────────────────────────────────────────────────────
# TestCase
# ─────────────────────────────────────────────────────────────────────────────

def save_test_case(tc: dict, created_by: str = "system") -> dict:
    base_id = tc["tc_id"]
    now = _now()

    with _get_driver().session() as session:
        cur = _current_version(session, "TestCase", base_id)
        new_v = cur + 1
        node_id = _vid(base_id, new_v)
        is_new = cur == 0

        if not is_new:
            _expire_current(session, "TestCase", base_id, now)

        session.run(
            """
            CREATE (n:TestCase {
                node_id:         $node_id,
                base_id:         $base_id,
                flow_id:         $flow_id,
                title:           $title,
                type:            $type,
                steps:           $steps,
                expected_result: $expected_result,
                version:         $version,
                status:          'active',
                is_current:      true,
                valid_at:        $now,
                invalid_at:      null,
                created_at:      $now,
                created_by:      $created_by
            })
            """,
            node_id=node_id, base_id=base_id,
            flow_id=tc.get("flow_id", ""),
            title=tc["title"],
            type=tc.get("type", "positive"),
            steps=json.dumps(tc.get("steps", [])),
            expected_result=tc.get("expected_result", ""),
            version=new_v, now=now, created_by=created_by,
        )

    return {"node_id": node_id, "version": new_v, "is_new": is_new}


def get_test_case(base_id: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:TestCase {base_id:$b, is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, "
            "n.flow_id AS flow_id, n.title AS title, n.type AS type, "
            "n.steps AS steps, n.expected_result AS expected_result, "
            "n.version AS version, n.valid_at AS valid_at",
            b=base_id,
        ).single()
        if not r:
            return None
        row = dict(r)
        if isinstance(row.get("steps"), str):
            row["steps"] = json.loads(row["steps"])
        return row


def get_all_test_cases() -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:TestCase {is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, "
            "n.flow_id AS flow_id, n.title AS title, n.type AS type, "
            "n.version AS version, n.valid_at AS valid_at"
        )]


def get_test_case_history(base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:TestCase {base_id:$b}) "
            "RETURN n.node_id AS node_id, n.version AS version, "
            "n.status AS status, n.valid_at AS valid_at, n.invalid_at AS invalid_at "
            "ORDER BY n.version",
            b=base_id,
        )]


# ─────────────────────────────────────────────────────────────────────────────
# Edge operations
# ─────────────────────────────────────────────────────────────────────────────

def create_edge(from_node_id: str, rel_type: str, to_node_id: str) -> bool:
    """
    Create a typed edge between two nodes (by node_id).
    Returns True if a new edge was created, False if it already existed.
    """
    if rel_type not in VALID_REL_TYPES:
        raise ValueError(f"Invalid rel type: {rel_type}")
    now = _now()
    with _get_driver().session() as session:
        result = session.run(
            f"""
            MATCH (a {{node_id: $a}})
            MATCH (b {{node_id: $b}})
            MERGE (a)-[r:{rel_type}]->(b)
            ON CREATE SET r.valid_at = $now, r.invalid_at = null
            RETURN (r.valid_at = $now) AS created
            """,
            a=from_node_id, b=to_node_id, now=now,
        )
        r = result.single()
        return bool(r["created"]) if r else False


def expire_edge(from_node_id: str, rel_type: str, to_node_id: str) -> None:
    if rel_type not in VALID_REL_TYPES:
        raise ValueError(f"Invalid rel type: {rel_type}")
    now = _now()
    with _get_driver().session() as session:
        session.run(
            f"""
            MATCH (a {{node_id:$a}})-[r:{rel_type}]->(b {{node_id:$b}})
            WHERE r.invalid_at IS NULL
            SET r.invalid_at = $now
            """,
            a=from_node_id, b=to_node_id, now=now,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Graph query helpers  (used by show-graph + delta commands)
# ─────────────────────────────────────────────────────────────────────────────

def get_connected_flows(story_node_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (us {node_id:$id})-[:HAS_FLOW]->(f:Flow {is_current:true}) "
            "RETURN f.node_id AS node_id, f.base_id AS base_id, "
            "f.title AS title, f.version AS version",
            id=story_node_id,
        )]


def get_connected_features(flow_node_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (f {node_id:$id})-[:USES_FEATURE]->(feat:Feature {is_current:true}) "
            "RETURN feat.node_id AS node_id, feat.base_id AS base_id, "
            "feat.name AS name, feat.version AS version",
            id=flow_node_id,
        )]


def get_connected_apis(feature_node_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (f {node_id:$id})-[:CALLS_API]->(ep:APIEndpoint {is_current:true}) "
            "RETURN ep.node_id AS node_id, ep.base_id AS base_id, "
            "ep.path AS path, ep.method AS method, ep.version AS version",
            id=feature_node_id,
        )]


def get_connected_test_cases(flow_node_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (f {node_id:$id})-[:HAS_TEST_CASE]->(tc:TestCase {is_current:true}) "
            "RETURN tc.node_id AS node_id, tc.base_id AS base_id, "
            "tc.title AS title, tc.type AS type, tc.version AS version",
            id=flow_node_id,
        )]


def get_depends_on(flow_node_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (f {node_id:$id})-[:DEPENDS_ON]->(dep:Flow {is_current:true}) "
            "RETURN dep.node_id AS node_id, dep.base_id AS base_id, dep.title AS title",
            id=flow_node_id,
        )]


def get_all_downstream(node_id: str) -> list[dict]:
    """
    Traverse all outgoing edges from a node and collect downstream nodes.
    Used for delta impact reporting.
    """
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            """
            MATCH (start {node_id: $id})-[*1..6]->(downstream)
            WHERE downstream.is_current = true
            RETURN DISTINCT
                labels(downstream)[0]  AS type,
                downstream.node_id     AS node_id,
                downstream.base_id     AS base_id,
                coalesce(downstream.title, downstream.name,
                         downstream.path, downstream.base_id) AS label
            """,
            id=node_id,
        )]


def get_node_props(node_id: str) -> dict:
    """Get all properties of any node by versioned node_id."""
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n {node_id: $id}) RETURN properties(n) AS props",
            id=node_id,
        ).single()
        return dict(r["props"]) if r else {}


def get_flows_depending_on(flow_base_id: str) -> list[dict]:
    """Current flows that DEPEND_ON a flow with this base_id."""
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (f:Flow {is_current:true})-[:DEPENDS_ON]->(dep:Flow {base_id:$b}) "
            "RETURN f.node_id AS node_id, f.base_id AS base_id, "
            "f.title AS title, f.version AS version",
            b=flow_base_id,
        )]


def get_flows_using_feature(feature_name: str) -> list[dict]:
    """Current flows that USES_FEATURE with given name (deduplicated)."""
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (f:Flow {is_current:true})-[:USES_FEATURE]->(feat:Feature {name:$name}) "
            "RETURN DISTINCT f.node_id AS node_id, f.base_id AS base_id, "
            "f.title AS title, f.version AS version",
            name=feature_name,
        )]


def get_features_calling_api(api_base_id: str) -> list[dict]:
    """Current features that CALLS_API with given base_id (deduplicated)."""
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (feat:Feature {is_current:true})-[:CALLS_API]->(ep:APIEndpoint {base_id:$b}) "
            "RETURN DISTINCT feat.node_id AS node_id, feat.base_id AS base_id, "
            "feat.name AS name, feat.version AS version",
            b=api_base_id,
        )]
