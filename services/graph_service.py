"""
Neo4j graph service — schema v2 (KnowledgeGraph_Schema reference).

Graph (no Flow nodes):
  (:UserStory)-[:HAS_FEATURE]->(:Feature)
  (:UserStory)-[:USES_API]->(:APIEndpoint)
  (:UserStory)-[:HAS_TEST_CASE]->(:TestCase)
  (:Feature)-[:USES_API {params, coupling_type}]->(:APIEndpoint)
  (:Feature)-[:NEXT_STEP]->(:Feature)
  (:Feature)-[:DEPENDS_ON]->(:Feature)
  (:Feature|:UserStory|:APIEndpoint)-[:HAS_TEST_CASE]->(:TestCase)
  (:APIEndpoint)-[:HAS_RESPONSE_SCHEMA]->(:APIResponseSchema)
  (:TestCase)-[:VALIDATES_AGAINST]->(:APIResponseSchema)

Flows = ordered list on UserStory.flows (LLM/heuristic derived).
"""

import json
import logging
from datetime import datetime, timezone

from neo4j import GraphDatabase

import config
from services.content_hash import (
    hash_api_endpoint,
    hash_feature,
    hash_response_schema,
    hash_test_case,
    hash_user_story,
)
from services.graph_model import (
    DEFAULT_COUPLING,
    DEFAULT_STATUS,
    REL_PREVIOUS_VERSION,
    STATUS_ARCHIVED,
    VALID_REL_TYPES,
)
from services.versioning_fields import (
    CYPHER_ACTIVE_EDGE,
    CYPHER_EDGE_CREATE_TEMPORAL,
    CYPHER_EXPIRE_NODE_SET,
    CYPHER_HISTORY_VALID,
)

logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

_driver = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _vid(base_id: str, version: int) -> str:
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
    result = session.run(
        f"MATCH (n:{label} {{base_id: $base_id, is_current: true}}) RETURN n.version AS v",
        base_id=base_id,
    )
    record = result.single()
    return int(record["v"]) if record else 0


def _max_version(session, label: str, base_id: str) -> int:
    """Highest version ever stored for base_id (including archived)."""
    record = session.run(
        f"MATCH (n:{label} {{base_id: $base_id}}) RETURN max(n.version) AS v",
        base_id=base_id,
    ).single()
    if record and record["v"] is not None:
        return int(record["v"])
    return 0


def _allocate_version(
    session,
    label: str,
    base_id: str,
    now: str,
    *,
    version_policy: str = "deprecate",
) -> tuple[int, str, bool, str | None]:
    """
    Allocate next version id for upload.

    version_policy:
      - deprecate: keep old versions archived, new becomes v+1
      - delete: hard-delete all previous versions for this base_id, new becomes v1
    """
    prev_node_id = None

    if version_policy == "delete":
        session.run(
            f"MATCH (n:{label} {{base_id: $base_id}}) DETACH DELETE n",
            base_id=base_id,
        )
        max_v = 0
        is_new = True
    else:
        max_v = _max_version(session, label, base_id)
        is_new = max_v == 0
        if _current_version(session, label, base_id) > 0:
            prev_node_id = _expire_current(session, label, base_id, now)

    new_v = max_v + 1
    node_id = _vid(base_id, new_v)
    while session.run(
        f"MATCH (n:{label} {{node_id: $nid}}) RETURN n LIMIT 1",
        nid=node_id,
    ).single():
        new_v += 1
        node_id = _vid(base_id, new_v)
    return new_v, node_id, is_new, prev_node_id


def _expire_current(session, label: str, base_id: str, now: str) -> str | None:
    """Archive current version and return archived node_id."""
    row = session.run(
        f"MATCH (n:{label} {{base_id: $base_id, is_current: true}}) RETURN n.node_id AS id LIMIT 1",
        base_id=base_id,
    ).single()
    extra = ", n.endpoint_id = n.node_id" if label == "APIEndpoint" else ""
    session.run(
        f"MATCH (n:{label} {{base_id: $base_id, is_current: true}}) "
        f"SET {CYPHER_EXPIRE_NODE_SET}{extra} "
        f"REMOVE n.valid_at, n.invalid_at",
        base_id=base_id,
        now=now,
    )
    return row["id"] if row else None


def _base_audit(now: str, created_by: str) -> dict:
    return {
        "status": DEFAULT_STATUS,
        "created_at": now,
        "created_by": created_by,
        "updated_at": now,
        "updated_by": created_by,
    }


def _track_version_saved(
    entity_type: str,
    base_id: str,
    data: dict,
    result: dict,
    *,
    content_hash: str | None,
    version_policy: str,
    created_by: str,
    valid_from: str | None = None,
) -> None:
    try:
        from services import tracking

        tracking.on_version_saved(
            entity_type,
            base_id,
            data,
            node_id=result["node_id"],
            version=result["version"],
            is_new=result.get("is_new", False),
            content_hash=content_hash,
            version_policy=version_policy,
            created_by=created_by,
            valid_from=valid_from,
        )
    except Exception as e:
        logger.warning("Tracking write skipped: %s", e)


def _link_previous_version(session, newer_node_id: str, older_node_id: str, now: str) -> None:
    if not older_node_id:
        return
    session.run(
        f"MATCH (n {{node_id:$newer}}), (o {{node_id:$older}}) "
        f"MERGE (n)-[r:{REL_PREVIOUS_VERSION}]->(o) "
        "ON CREATE SET r.valid_from=$now, r.valid_to=null, r.status=$status",
        newer=newer_node_id,
        older=older_node_id,
        now=now,
        status=DEFAULT_STATUS,
    )


# ─── Content hash lookup ─────────────────────────────────────────────────────

def find_by_content_hash(label: str, content_hash: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            f"MATCH (n:{label} {{content_hash: $h, is_current: true}}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.version AS version, "
            "n.title AS title, n.name AS name LIMIT 1",
            h=content_hash,
        ).single()
        return dict(r) if r else None


def find_by_openapi_bundle_hash(bundle_hash: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:APIEndpoint {openapi_bundle_hash: $h, is_current: true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.path AS path, "
            "n.method AS method, n.version AS version LIMIT 1",
            h=bundle_hash,
        ).single()
        return dict(r) if r else None


# ─── UserStory ───────────────────────────────────────────────────────────────

def save_user_story(
    story: dict,
    created_by: str = "system",
    *,
    version_policy: str = "deprecate",
) -> dict:
    base_id = story["story_id"]
    now = _now()
    flows = story.get("flows") or []
    content_hash = hash_user_story(story)

    with _get_driver().session() as session:
        new_v, node_id, is_new, prev_node_id = _allocate_version(
            session, "UserStory", base_id, now, version_policy=version_policy
        )

        session.run(
            """
            CREATE (n:UserStory {
                node_id: $node_id, base_id: $base_id,
                title: $title, content: $content, flows: $flows,
                depends_on: $depends_on, blocked_by: $blocked_by,
                content_hash: $content_hash,
                version: $version, is_current: true,
                valid_from: $now, valid_to: null,
                status: $status,
                created_at: $created_at, created_by: $created_by,
                updated_at: $updated_at, updated_by: $updated_by
            })
            """,
            node_id=node_id,
            base_id=base_id,
            title=story["title"],
            content=story.get("content", ""),
            flows=flows,
            depends_on=story.get("depends_on", []),
            blocked_by=story.get("blocked_by", []),
            content_hash=content_hash,
            version=new_v,
            now=now,
            **_base_audit(now, created_by),
        )
        if version_policy != "delete":
            _link_previous_version(session, node_id, prev_node_id, now)
    result = {"node_id": node_id, "version": new_v, "is_new": is_new, "flows": flows}
    _track_version_saved(
        "user_story",
        base_id,
        story,
        result,
        content_hash=content_hash,
        version_policy=version_policy,
        created_by=created_by,
        valid_from=now,
    )
    return result


def get_user_story(base_id: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:UserStory {base_id:$b, is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.title AS title, "
            "n.content AS content, n.flows AS flows, n.version AS version, "
            "n.depends_on AS depends_on, n.blocked_by AS blocked_by, "
            "coalesce(n.valid_from, n.valid_at) AS valid_from",
            b=base_id,
        ).single()
        return dict(r) if r else None


def get_all_user_stories() -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:UserStory {is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.title AS title, "
            "n.flows AS flows, n.version AS version"
        )]


def get_user_story_history(base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:UserStory {base_id:$b}) "
            "RETURN n.node_id AS node_id, n.version AS version, n.status AS status, "
            f"{CYPHER_HISTORY_VALID} "
            "ORDER BY n.version",
            b=base_id,
        )]


# ─── Feature ─────────────────────────────────────────────────────────────────

def save_feature(
    feature: dict,
    created_by: str = "system",
    *,
    version_policy: str = "deprecate",
) -> dict:
    base_id = feature["feature_id"]
    now = _now()
    content_hash = hash_feature(feature)

    with _get_driver().session() as session:
        new_v, node_id, is_new, prev_node_id = _allocate_version(
            session, "Feature", base_id, now, version_policy=version_policy
        )

        session.run(
            """
            CREATE (n:Feature {
                node_id: $node_id, base_id: $base_id,
                name: $name, description: $description, apis_used: $apis_used,
                depends_on: $depends_on, order: $order,
                content_hash: $content_hash,
                version: $version, is_current: true,
                valid_from: $now, valid_to: null,
                status: $status,
                created_at: $created_at, created_by: $created_by,
                updated_at: $updated_at, updated_by: $updated_by
            })
            """,
            node_id=node_id,
            base_id=base_id,
            name=feature["name"],
            description=feature.get("description", ""),
            apis_used=feature.get("apis_used", []),
            depends_on=feature.get("depends_on", []),
            order=feature.get("order", 0),
            content_hash=content_hash,
            version=new_v,
            now=now,
            **_base_audit(now, created_by),
        )
        if version_policy != "delete":
            _link_previous_version(session, node_id, prev_node_id, now)
    result = {"node_id": node_id, "version": new_v, "is_new": is_new}
    _track_version_saved(
        "feature",
        base_id,
        feature,
        result,
        content_hash=content_hash,
        version_policy=version_policy,
        created_by=created_by,
        valid_from=now,
    )
    return result


def get_feature(base_id: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:Feature {base_id:$b, is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.name AS name, "
            "n.description AS description, n.apis_used AS apis_used, "
            "n.depends_on AS depends_on, n.version AS version",
            b=base_id,
        ).single()
        return dict(r) if r else None


def get_feature_by_name(name: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:Feature {name:$name, is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.name AS name, "
            "n.apis_used AS apis_used, n.version AS version LIMIT 1",
            name=name,
        ).single()
        return dict(r) if r else None


def get_all_features() -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:Feature {is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.name AS name, "
            "n.apis_used AS apis_used, n.version AS version"
        )]


def get_feature_history(base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:Feature {base_id:$b}) "
            "RETURN n.node_id AS node_id, n.version AS version, n.status AS status, "
            f"{CYPHER_HISTORY_VALID} "
            "ORDER BY n.version",
            b=base_id,
        )]


# ─── APIEndpoint ─────────────────────────────────────────────────────────────

def save_endpoint(
    endpoint: dict,
    created_by: str = "system",
    *,
    openapi_bundle_hash: str | None = None,
    version_policy: str = "deprecate",
) -> dict:
    base_id = f"{endpoint['method'].upper()}:{endpoint['path']}"
    now = _now()
    content_hash = hash_api_endpoint(endpoint)

    with _get_driver().session() as session:
        new_v, node_id, is_new, prev_node_id = _allocate_version(
            session, "APIEndpoint", base_id, now, version_policy=version_policy
        )

        session.run(
            """
            CREATE (n:APIEndpoint {
                node_id: $node_id, base_id: $base_id,
                endpoint_id: $base_id, path: $path, method: $method,
                summary: $summary, request_schema: $request_schema,
                content_hash: $content_hash,
                openapi_bundle_hash: $openapi_bundle_hash,
                version: $version, is_current: true,
                valid_from: $now, valid_to: null,
                status: $status,
                created_at: $created_at, created_by: $created_by,
                updated_at: $updated_at, updated_by: $updated_by
            })
            """,
            node_id=node_id,
            base_id=base_id,
            path=endpoint["path"],
            method=endpoint["method"].upper(),
            summary=endpoint.get("summary", ""),
            request_schema=json.dumps(endpoint.get("request_schema", {})),
            content_hash=content_hash,
            openapi_bundle_hash=openapi_bundle_hash or "",
            version=new_v,
            now=now,
            **_base_audit(now, created_by),
        )
        if version_policy != "delete":
            _link_previous_version(session, node_id, prev_node_id, now)
    result = {"node_id": node_id, "base_id": base_id, "version": new_v, "is_new": is_new}
    _track_version_saved(
        "api_endpoint",
        base_id,
        endpoint,
        result,
        content_hash=content_hash,
        version_policy=version_policy,
        created_by=created_by,
        valid_from=now,
    )
    return result


def get_endpoint_by_path(path: str, method: str | None = None) -> dict | None:
    base_id = f"{method.upper()}:{path}" if method else None
    with _get_driver().session() as session:
        if base_id:
            r = session.run(
                "MATCH (n:APIEndpoint {base_id:$b, is_current:true}) "
                "RETURN n.node_id AS node_id, n.base_id AS base_id, n.path AS path, "
                "n.method AS method, n.summary AS summary, n.version AS version",
                b=base_id,
            ).single()
        else:
            r = session.run(
                "MATCH (n:APIEndpoint {path:$path, is_current:true}) "
                "RETURN n.node_id AS node_id, n.base_id AS base_id, n.path AS path, "
                "n.method AS method, n.summary AS summary, n.version AS version LIMIT 1",
                path=path,
            ).single()
        return dict(r) if r else None


def get_all_endpoints() -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:APIEndpoint {is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.path AS path, "
            "n.method AS method, n.summary AS summary, n.version AS version"
        )]


def get_endpoint_history(base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:APIEndpoint {base_id:$b}) "
            "RETURN n.node_id AS node_id, n.version AS version, n.status AS status, "
            f"{CYPHER_HISTORY_VALID} "
            "ORDER BY n.version",
            b=base_id,
        )]


# ─── APIResponseSchema ───────────────────────────────────────────────────────

def save_response_schema(
    schema: dict,
    created_by: str = "system",
    *,
    version_policy: str = "deprecate",
) -> dict:
    ep = schema["endpoint_id"]
    sc = schema["status_code"]
    label = schema.get("outcome_label", "default")
    base_id = schema.get("schema_id") or f"{ep}#{sc}#{label}"
    now = _now()
    content_hash = hash_response_schema(schema)

    with _get_driver().session() as session:
        new_v, node_id, is_new, prev_node_id = _allocate_version(
            session, "APIResponseSchema", base_id, now, version_policy=version_policy
        )

        session.run(
            """
            CREATE (n:APIResponseSchema {
                node_id: $node_id, base_id: $base_id,
                endpoint_id: $endpoint_id, status_code: $status_code,
                outcome_label: $outcome_label, schema: $schema,
                description: $description,
                content_hash: $content_hash,
                version: $version, is_current: true,
                valid_from: $now, valid_to: null,
                status: $status,
                created_at: $created_at, created_by: $created_by,
                updated_at: $updated_at, updated_by: $updated_by
            })
            """,
            node_id=node_id,
            base_id=base_id,
            endpoint_id=ep,
            status_code=int(sc),
            outcome_label=label,
            schema=json.dumps(schema.get("schema", {})),
            description=schema.get("description", ""),
            content_hash=content_hash,
            version=new_v,
            now=now,
            **_base_audit(now, created_by),
        )
        if version_policy != "delete":
            _link_previous_version(session, node_id, prev_node_id, now)
    result = {"node_id": node_id, "base_id": base_id, "version": new_v, "is_new": is_new}
    _track_version_saved(
        "api_response_schema",
        base_id,
        schema,
        result,
        content_hash=content_hash,
        version_policy=version_policy,
        created_by=created_by,
        valid_from=now,
    )
    return result


def get_response_schemas_for_endpoint(endpoint_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:APIResponseSchema {endpoint_id:$e, is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, "
            "n.status_code AS status_code, n.outcome_label AS outcome_label",
            e=endpoint_id,
        )]


# ─── TestCase ────────────────────────────────────────────────────────────────

def save_test_case(
    tc: dict,
    created_by: str = "system",
    *,
    version_policy: str = "deprecate",
) -> dict:
    base_id = tc["tc_id"]
    now = _now()
    content_hash = hash_test_case(tc)

    with _get_driver().session() as session:
        new_v, node_id, is_new, prev_node_id = _allocate_version(
            session, "TestCase", base_id, now, version_policy=version_policy
        )

        session.run(
            """
            CREATE (n:TestCase {
                node_id: $node_id, base_id: $base_id,
                title: $title, type: $type, test_layer: $test_layer,
                linked_to: $linked_to,
                steps: $steps, expected_result: $expected_result,
                content_hash: $content_hash,
                version: $version, is_current: true,
                valid_from: $now, valid_to: null,
                status: $status,
                created_at: $created_at, created_by: $created_by,
                updated_at: $updated_at, updated_by: $updated_by
            })
            """,
            node_id=node_id,
            base_id=base_id,
            title=tc["title"],
            type=tc.get("type", "positive"),
            test_layer=tc.get("test_layer", "api"),
            linked_to=tc.get("linked_to", ""),
            steps=json.dumps(tc.get("steps", [])),
            expected_result=tc.get("expected_result", ""),
            content_hash=content_hash,
            version=new_v,
            now=now,
            **_base_audit(now, created_by),
        )
        if version_policy != "delete":
            _link_previous_version(session, node_id, prev_node_id, now)
    result = {"node_id": node_id, "version": new_v, "is_new": is_new}
    _track_version_saved(
        "test_case",
        base_id,
        tc,
        result,
        content_hash=content_hash,
        version_policy=version_policy,
        created_by=created_by,
        valid_from=now,
    )
    return result


def get_test_case(base_id: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:TestCase {base_id:$b, is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.title AS title, "
            "n.type AS type, n.test_layer AS test_layer, n.linked_to AS linked_to, "
            "n.steps AS steps, n.expected_result AS expected_result, n.version AS version",
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
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.title AS title, "
            "n.type AS type, n.linked_to AS linked_to, n.version AS version"
        )]


def get_test_case_history(base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:TestCase {base_id:$b}) "
            "RETURN n.node_id AS node_id, n.version AS version, n.status AS status, "
            f"{CYPHER_HISTORY_VALID} "
            "ORDER BY n.version",
            b=base_id,
        )]


# ─── Edges ───────────────────────────────────────────────────────────────────

def prune_edges_on_archived_nodes() -> int:
    """Remove relationships touching non-current nodes (leftover after versioning)."""
    with _get_driver().session() as session:
        result = session.run(
            """
            MATCH (a)-[r]->(b)
            WHERE coalesce(a.is_current, true) = false OR coalesce(b.is_current, true) = false
            DELETE r
            """
        )
        summary = result.consume()
        return summary.counters.relationships_deleted


def create_edge(
    from_node_id: str,
    rel_type: str,
    to_node_id: str,
    *,
    params: str | None = None,
    coupling_type: str | None = None,
) -> bool:
    if rel_type not in VALID_REL_TYPES:
        raise ValueError(f"Invalid rel type: {rel_type}")
    now = _now()
    props = CYPHER_EDGE_CREATE_TEMPORAL.strip()
    extra = {"status": DEFAULT_STATUS}
    if rel_type == "USES_API":
        props += ", r.params = $params, r.coupling_type = $coupling_type"
        extra["params"] = params or ""
        extra["coupling_type"] = coupling_type or DEFAULT_COUPLING

    with _get_driver().session() as session:
        result = session.run(
            f"""
            MATCH (a {{node_id: $a}}) MATCH (b {{node_id: $b}})
            MERGE (a)-[r:{rel_type}]->(b)
            ON CREATE SET {props}
            RETURN (r.valid_from = $now) AS created
            """,
            a=from_node_id,
            b=to_node_id,
            now=now,
            **extra,
        )
        r = result.single()
        return bool(r["created"]) if r else False


def delete_edge(from_node_id: str, rel_type: str, to_node_id: str) -> dict:
    if rel_type not in VALID_REL_TYPES:
        raise ValueError(f"Invalid rel type: {rel_type}")
    with _get_driver().session() as session:
        result = session.run(
            f"MATCH (a {{node_id:$a}})-[r:{rel_type}]->(b {{node_id:$b}}) DELETE r RETURN count(r) AS n",
            a=from_node_id,
            b=to_node_id,
        )
        deleted = result.single()["n"] > 0
    return {"deleted": deleted}


def delete_node(entity_type: str, base_id: str) -> dict:
    """Delete all versions of a node; DETACH DELETE removes every relationship."""
    label_map = {
        "user_story": "UserStory",
        "feature": "Feature",
        "api_endpoint": "APIEndpoint",
        "api_response_schema": "APIResponseSchema",
        "test_case": "TestCase",
    }
    label = label_map.get(entity_type)
    if not label:
        raise ValueError(f"Unknown entity type: {entity_type}")

    with _get_driver().session() as session:
        count = session.run(
            f"MATCH (n:{label} {{base_id: $b}}) RETURN count(n) AS c",
            b=base_id,
        ).single()["c"]
        if count == 0:
            return {"deleted": False, "reason": "not_found"}
        session.run(f"MATCH (n:{label} {{base_id: $b}}) DETACH DELETE n", b=base_id)
    return {
        "deleted": True,
        "base_id": base_id,
        "entity_type": entity_type,
        "versions_removed": count,
    }


def list_current_nodes() -> dict:
    """Inventory of all current (is_current) nodes in the knowledge graph."""
    labels = ["UserStory", "Feature", "APIEndpoint", "APIResponseSchema", "TestCase"]
    nodes = []
    by_type: dict[str, int] = {}

    with _get_driver().session() as session:
        rows = session.run(
            """
            MATCH (n)
            WHERE n.is_current = true
              AND any(l IN labels(n) WHERE l IN $labels)
            RETURN labels(n)[0] AS type, n.node_id AS id, n.base_id AS base_id,
                   n.version AS version, properties(n) AS props
            ORDER BY type, n.base_id
            """,
            labels=labels,
        )
        for row in rows:
            ntype = row["type"]
            props = dict(row["props"] or {})
            by_type[ntype] = by_type.get(ntype, 0) + 1
            nodes.append({
                "id": row["id"],
                "base_id": row["base_id"],
                "type": ntype,
                "entity_type": LABEL_TO_TYPE.get(ntype, ntype.lower()),
                "label": _node_display_label(props),
                "version": row["version"],
            })

    return {"total": len(nodes), "by_type": by_type, "nodes": nodes}


def clear_knowledge_graph() -> dict:
    """Remove every knowledge-graph node and all relationships."""
    labels = ["UserStory", "Feature", "APIEndpoint", "APIResponseSchema", "TestCase"]
    with _get_driver().session() as session:
        r = session.run(
            """
            MATCH (n)
            WHERE any(l IN labels(n) WHERE l IN $labels)
            RETURN count(n) AS c
            """,
            labels=labels,
        ).single()
        deleted = int(r["c"]) if r else 0
        if deleted:
            session.run(
                """
                MATCH (n)
                WHERE any(l IN labels(n) WHERE l IN $labels)
                DETACH DELETE n
                """,
                labels=labels,
            )
    return {"cleared": True, "nodes_deleted": deleted}


# ─── Traversal helpers ───────────────────────────────────────────────────────

def get_features_for_story(story_node_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (s {node_id:$id})-[:HAS_FEATURE]->(f:Feature {is_current:true}) "
            "RETURN f.node_id AS node_id, f.base_id AS base_id, f.name AS name, f.version AS version",
            id=story_node_id,
        )]


def get_apis_for_feature(feature_node_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (f {node_id:$id})-[r:USES_API]->(ep:APIEndpoint {is_current:true}) "
            "RETURN ep.node_id AS node_id, ep.base_id AS base_id, ep.path AS path, "
            "ep.method AS method, r.params AS params, r.coupling_type AS coupling_type",
            id=feature_node_id,
        )]


def get_test_cases_for_entity(node_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (e {node_id:$id})-[:HAS_TEST_CASE]->(tc:TestCase {is_current:true}) "
            "RETURN tc.node_id AS node_id, tc.base_id AS base_id, tc.title AS title, tc.type AS type",
            id=node_id,
        )]


def get_features_using_api(api_base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (feat:Feature {is_current:true})-[:USES_API]->(ep:APIEndpoint {base_id:$b}) "
            "RETURN DISTINCT feat.node_id AS node_id, feat.base_id AS base_id, feat.name AS name",
            b=api_base_id,
        )]


def get_stories_linking_feature(feature_base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (s:UserStory {is_current:true})-[:HAS_FEATURE]->(f:Feature {base_id:$b}) "
            "RETURN DISTINCT s.node_id AS node_id, s.base_id AS base_id, s.title AS title",
            b=feature_base_id,
        )]


def get_node_props(node_id: str) -> dict:
    with _get_driver().session() as session:
        r = session.run("MATCH (n {node_id:$id}) RETURN properties(n) AS p", id=node_id).single()
        return dict(r["p"]) if r else {}


def resolve_entity(linked_to: str) -> tuple[str, dict] | None:
    """Resolve linked_to id to (label, node dict)."""
    for label, fn in (
        ("UserStory", get_user_story),
        ("Feature", get_feature),
    ):
        n = fn(linked_to)
        if n:
            return label, n
    if ":" in linked_to:
        ep = get_endpoint_by_path(linked_to.split(":", 1)[1], linked_to.split(":", 1)[0])
    else:
        ep = get_endpoint_by_path(linked_to) or get_endpoint_by_path(linked_to, "GET")
    if ep:
        return "APIEndpoint", ep
    feat = get_feature_by_name(linked_to)
    if feat:
        return "Feature", feat
    return None


# ─── Graph export ──────────────────────────────────────────────────────────────

LABEL_TO_TYPE = {
    "UserStory": "user_story",
    "Feature": "feature",
    "APIEndpoint": "api_endpoint",
    "APIResponseSchema": "api_response_schema",
    "TestCase": "test_case",
}


def _node_display_label(props: dict) -> str:
    return (
        props.get("title")
        or props.get("name")
        or props.get("outcome_label")
        or (f"{props.get('method', '')} {props.get('path', '')}".strip())
        or props.get("base_id")
        or "?"
    )


def _serialize_props(props: dict) -> dict:
    out = {}
    skip = {
        "node_id", "is_current", "valid_at", "invalid_at",
        "valid_from", "valid_to", "created_at", "created_by",
    }
    for k, v in props.items():
        if k in skip:
            continue
        if isinstance(v, str) and k in ("steps", "request_schema", "schema"):
            try:
                out[k] = json.loads(v)
            except json.JSONDecodeError:
                out[k] = v
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def get_full_graph(story_base_id: str | None = None) -> dict:
    nodes, edges = [], []
    seen_n, seen_e = set(), set()

    with _get_driver().session() as session:
        if story_base_id:
            rows = session.run(
                """
                MATCH (s:UserStory {base_id: $bid, is_current: true})
                OPTIONAL MATCH (s)-[*0..5]-(n)
                WHERE n IS NULL OR n.is_current = true
                WITH collect(DISTINCT s) + collect(DISTINCT n) AS all_nodes
                UNWIND all_nodes AS node
                WITH node WHERE node IS NOT NULL
                RETURN labels(node)[0] AS type, node.node_id AS id, node.base_id AS base_id,
                       node.version AS version, properties(node) AS props
                """,
                bid=story_base_id,
            )
        else:
            rows = session.run(
                """
                MATCH (n) WHERE n.is_current = true
                AND any(l IN labels(n) WHERE l IN
                  ['UserStory','Feature','APIEndpoint','APIResponseSchema','TestCase'])
                RETURN labels(n)[0] AS type, n.node_id AS id, n.base_id AS base_id,
                       n.version AS version, properties(n) AS props
                """
            )

        for row in rows:
            nid = row["id"]
            if not nid or nid in seen_n:
                continue
            seen_n.add(nid)
            props = dict(row["props"] or {})
            ntype = row["type"]
            nodes.append({
                "id": nid,
                "base_id": row["base_id"],
                "type": ntype,
                "entity_type": LABEL_TO_TYPE.get(ntype, ntype.lower()),
                "label": _node_display_label(props),
                "version": row["version"],
                "properties": _serialize_props(props),
            })

        if seen_n:
            for row in session.run(
                f"""
                MATCH (a)-[r]->(b)
                WHERE a.node_id IN $ids AND b.node_id IN $ids AND {CYPHER_ACTIVE_EDGE}
                RETURN a.node_id AS source, b.node_id AS target, type(r) AS rel_type
                """,
                ids=list(seen_n),
            ):
                key = f"{row['source']}|{row['rel_type']}|{row['target']}"
                if key in seen_e:
                    continue
                seen_e.add(key)
                edges.append({
                    "id": key,
                    "source": row["source"],
                    "target": row["target"],
                    "rel_type": row["rel_type"],
                })

    return {"nodes": nodes, "edges": edges, "story_id": story_base_id}


def check_connection() -> dict:
    try:
        with _get_driver().session() as session:
            r = session.run("RETURN 1 AS ok").single()
            return {"connected": bool(r and r["ok"] == 1)}
    except Exception as e:
        return {"connected": False, "error": str(e)}
