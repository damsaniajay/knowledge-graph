"""
Neo4j graph service — schema v2 (KnowledgeGraph_Schema reference).

Graph (no Flow nodes):
  (:UserStory)-[:HAS_FEATURE]->(:Feature)
  (:UserStory)-[:USES_API]->(:APIEndpoint)
  (:UserStory)-[:HAS_TEST_CASE]->(:TestCase)
  (:Feature)-[:USES_API {params, coupling_type}]->(:APIEndpoint)
  (:Feature)-[:DEPENDS_ON]->(:Feature)   # dependent → prerequisite (from flows[] order)
  (:Feature|:UserStory|:APIEndpoint)-[:HAS_TEST_CASE]->(:TestCase)
  (:APIEndpoint)-[:HAS_RESPONSE_SCHEMA]->(:APIResponseSchema)
  (:TestCase)-[:DEPENDS_ON]->(:TestCase)      # dependent → prerequisite (execution order)
  (:TestCase)-[:DEPENDENCY]->(:TestCase)     # prerequisite → dependent (impact query)
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
    REL_BLOCKS,
    REL_DEPENDS_ON,
    REL_HAS_FEATURE,
    REL_HAS_TEST_CASE,
    REL_PREVIOUS_VERSION,
    REL_USES_API,
    STATUS_ARCHIVED,
    VALID_REL_TYPES,
)

# Only the live UserStory should own these; archived versions must not keep them.
_ARCHIVED_STORY_PRODUCT_RELS = (
    REL_HAS_FEATURE,
    REL_USES_API,
    REL_HAS_TEST_CASE,
    REL_DEPENDS_ON,
    REL_BLOCKS,
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
      - deprecate: archive current, new becomes v+1 (version history)
      - replace: first version only at allocate time (no current row to archive)
    """
    if version_policy == "delete":
        raise ValueError("version_policy 'delete' is not allowed; version history is always kept")

    if version_policy == "replace":
        version_policy = "deprecate"

    prev_node_id = None
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
    extra = ""
    if label == "APIEndpoint":
        extra = ", n.endpoint_id = n.node_id"
    elif label == "Feature":
        # Free Feature.name for the next version (legacy DBs had UNIQUE(name)).
        extra = ", n.archived_name = coalesce(n.archived_name, n.name), n.name = n.node_id"
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
    version_policy: str = "replace",
) -> dict:
    base_id = story["story_id"]
    now = _now()
    flows = story.get("flows") or []
    content_hash = hash_user_story(story)

    with _get_driver().session() as session:
        if version_policy == "replace":
            row = session.run(
                "MATCH (n:UserStory {base_id:$b, is_current:true}) "
                "RETURN n.node_id AS node_id, n.version AS version LIMIT 1",
                b=base_id,
            ).single()
            if row:
                node_id = row["node_id"]
                new_v = row["version"]
                session.run(
                    """
                    MATCH (n:UserStory {node_id: $node_id})
                    SET n.title = $title, n.content = $content, n.flows = $flows,
                        n.depends_on = $depends_on, n.blocked_by = $blocked_by,
                        n.content_hash = $content_hash,
                        n.updated_at = $now, n.updated_by = $updated_by
                    """,
                    node_id=node_id,
                    title=story["title"],
                    content=story.get("content", ""),
                    flows=flows,
                    depends_on=story.get("depends_on", []),
                    blocked_by=story.get("blocked_by", []),
                    content_hash=content_hash,
                    now=now,
                    updated_by=created_by,
                )
                result = {
                    "node_id": node_id,
                    "version": new_v,
                    "is_new": False,
                    "flows": flows,
                    "replaced": True,
                }
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
        if version_policy == "deprecate" and prev_node_id:
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


def get_current_content_hash(label: str, base_id: str) -> str | None:
    with _get_driver().session() as session:
        r = session.run(
            f"MATCH (n:{label} {{base_id:$b, is_current:true}}) "
            "RETURN n.content_hash AS h LIMIT 1",
            b=base_id,
        ).single()
        return r["h"] if r else None


def get_user_story(base_id: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:UserStory {base_id:$b, is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.title AS title, "
            "n.content AS content, n.flows AS flows, n.version AS version, "
            "n.content_hash AS content_hash, "
            "n.depends_on AS depends_on, n.blocked_by AS blocked_by, "
            "coalesce(n.valid_from, n.valid_at) AS valid_from",
            b=base_id,
        ).single()
        return dict(r) if r else None


def get_all_user_stories() -> list[dict]:
    """Current (active) user story versions only — used for linking and identity."""
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:UserStory {is_current:true}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.title AS title, "
            "n.flows AS flows, n.version AS version"
        )]


def list_user_story_versions() -> list[dict]:
    """Every UserStory version (current + archived) for UI dropdown and graph."""
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (n:UserStory) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.title AS title, "
            "n.flows AS flows, n.version AS version, n.is_current AS is_current, "
            "n.status AS status "
            "ORDER BY n.base_id, n.version"
        )]


def get_user_story_version(node_id: str) -> dict | None:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:UserStory {node_id:$id}) "
            "RETURN n.node_id AS node_id, n.base_id AS base_id, n.title AS title, "
            "n.content AS content, n.flows AS flows, n.version AS version, "
            "n.is_current AS is_current, n.status AS status, "
            "coalesce(n.valid_from, n.valid_at) AS valid_from",
            id=node_id,
        ).single()
        return dict(r) if r else None


def user_story_base_id_exists(base_id: str) -> bool:
    with _get_driver().session() as session:
        r = session.run(
            "MATCH (n:UserStory {base_id:$b}) RETURN n LIMIT 1",
            b=base_id,
        ).single()
        return bool(r)


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
    version_policy: str = "replace",
) -> dict:
    base_id = feature["feature_id"]
    now = _now()
    content_hash = hash_feature(feature)

    with _get_driver().session() as session:
        if version_policy == "replace":
            row = session.run(
                "MATCH (n:Feature {base_id:$b, is_current:true}) "
                "RETURN n.node_id AS node_id, n.version AS version LIMIT 1",
                b=base_id,
            ).single()
            if row:
                node_id = row["node_id"]
                new_v = row["version"]
                session.run(
                    """
                    MATCH (n:Feature {node_id: $node_id})
                    SET n.name = $name, n.description = $description,
                        n.apis_used = $apis_used, n.depends_on = $depends_on,
                        n.order = $order, n.content_hash = $content_hash,
                        n.updated_at = $now, n.updated_by = $updated_by
                    """,
                    node_id=node_id,
                    name=feature["name"],
                    description=feature.get("description", ""),
                    apis_used=feature.get("apis_used", []),
                    depends_on=feature.get("depends_on", []),
                    order=feature.get("order", 0),
                    content_hash=content_hash,
                    now=now,
                    updated_by=created_by,
                )
                result = {
                    "node_id": node_id,
                    "version": new_v,
                    "is_new": False,
                    "replaced": True,
                }
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
        if version_policy == "deprecate" and prev_node_id:
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
    version_policy: str = "replace",
) -> dict:
    base_id = f"{endpoint['method'].upper()}:{endpoint['path']}"
    now = _now()
    content_hash = hash_api_endpoint(endpoint)

    with _get_driver().session() as session:
        if version_policy == "replace":
            row = session.run(
                "MATCH (n:APIEndpoint {base_id:$b, is_current:true}) "
                "RETURN n.node_id AS node_id, n.version AS version LIMIT 1",
                b=base_id,
            ).single()
            if row:
                node_id = row["node_id"]
                new_v = row["version"]
                session.run(
                    """
                    MATCH (n:APIEndpoint {node_id: $node_id})
                    SET n.path = $path, n.method = $method, n.summary = $summary,
                        n.request_schema = $request_schema, n.content_hash = $content_hash,
                        n.openapi_bundle_hash = $openapi_bundle_hash,
                        n.updated_at = $now, n.updated_by = $updated_by
                    """,
                    node_id=node_id,
                    path=endpoint["path"],
                    method=endpoint["method"].upper(),
                    summary=endpoint.get("summary", ""),
                    request_schema=json.dumps(endpoint.get("request_schema", {})),
                    content_hash=content_hash,
                    openapi_bundle_hash=openapi_bundle_hash or "",
                    now=now,
                    updated_by=created_by,
                )
                result = {
                    "node_id": node_id,
                    "base_id": base_id,
                    "version": new_v,
                    "is_new": False,
                    "replaced": True,
                }
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
        if version_policy == "deprecate" and prev_node_id:
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
        if version_policy == "deprecate" and prev_node_id:
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
    version_policy: str = "replace",
) -> dict:
    base_id = tc["tc_id"]
    now = _now()
    content_hash = hash_test_case(tc)

    with _get_driver().session() as session:
        if version_policy == "replace":
            row = session.run(
                "MATCH (n:TestCase {base_id:$b, is_current:true}) "
                "RETURN n.node_id AS node_id, n.version AS version LIMIT 1",
                b=base_id,
            ).single()
            if row:
                node_id = row["node_id"]
                new_v = row["version"]
                session.run(
                    """
                    MATCH (n:TestCase {node_id: $node_id})
                    SET n.title = $title, n.type = $type, n.test_layer = $test_layer,
                        n.linked_to = $linked_to, n.steps = $steps,
                        n.depends_on_test_cases = $depends_on_test_cases,
                        n.expected_result = $expected_result,
                        n.content_hash = $content_hash,
                        n.updated_at = $now, n.updated_by = $updated_by
                    """,
                    node_id=node_id,
                    title=tc["title"],
                    type=tc.get("type", "positive"),
                    test_layer=tc.get("test_layer", "api"),
                    linked_to=tc.get("linked_to", ""),
                    steps=json.dumps(tc.get("steps", [])),
                    depends_on_test_cases=list(tc.get("depends_on_test_cases") or []),
                    expected_result=tc.get("expected_result", ""),
                    content_hash=content_hash,
                    now=now,
                    updated_by=created_by,
                )
                result = {
                    "node_id": node_id,
                    "version": new_v,
                    "is_new": False,
                    "replaced": True,
                }
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

        new_v, node_id, is_new, prev_node_id = _allocate_version(
            session, "TestCase", base_id, now, version_policy=version_policy
        )

        session.run(
            """
            CREATE (n:TestCase {
                node_id: $node_id, base_id: $base_id,
                title: $title, type: $type, test_layer: $test_layer,
                linked_to: $linked_to,
                depends_on_test_cases: $depends_on_test_cases,
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
            depends_on_test_cases=list(tc.get("depends_on_test_cases") or []),
            steps=json.dumps(tc.get("steps", [])),
            expected_result=tc.get("expected_result", ""),
            content_hash=content_hash,
            version=new_v,
            now=now,
            **_base_audit(now, created_by),
        )
        if version_policy == "deprecate" and prev_node_id:
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
            "n.depends_on_test_cases AS depends_on_test_cases, "
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
            "n.type AS type, n.linked_to AS linked_to, "
            "n.depends_on_test_cases AS depends_on_test_cases, "
            "n.version AS version"
        )]


def get_test_case_impact(base_id: str, *, max_hops: int = 10) -> dict:
    """
    Impact analysis for a test case via DEPENDENCY edges (prerequisite → dependent).

    Answers: which test cases directly or indirectly need this one?
    """
    root = get_test_case(base_id)
    if not root:
        return {"found": False, "base_id": base_id, "prerequisites": [], "dependents": []}

    prerequisites: list[dict] = []
    dependents: list[dict] = []

    with _get_driver().session() as session:
        for row in session.run(
            "MATCH (root:TestCase {base_id: $bid, is_current: true})"
            "-[:DEPENDS_ON]->(pre:TestCase) "
            "RETURN pre.base_id AS base_id, pre.title AS title",
            bid=base_id,
        ):
            prerequisites.append({"base_id": row["base_id"], "title": row["title"]})

        depth = max(1, min(int(max_hops), 25))
        # Neo4j does not allow parameters in variable-length relationship bounds.
        chain_cypher = (
            "MATCH (root:TestCase {base_id: $bid, is_current: true}) "
            f"MATCH path = (root)-[:DEPENDENCY*1..{depth}]->(impacted:TestCase) "
            "RETURN impacted.base_id AS base_id, impacted.title AS title, "
            "length(path) AS hops, "
            "[n IN nodes(path) | n.base_id] AS chain "
            "ORDER BY hops, impacted.base_id"
        )
        for row in session.run(chain_cypher, bid=base_id):
            chain = list(row["chain"] or [])
            dependents.append({
                "base_id": row["base_id"],
                "title": row["title"],
                "hops": row["hops"],
                "chain": chain,
                "prerequisite": chain[-2] if len(chain) >= 2 else base_id,
            })

    return {
        "found": True,
        "base_id": base_id,
        "node_id": root.get("node_id"),
        "title": root.get("title"),
        "prerequisites": prerequisites,
        "dependents": dependents,
        "dependent_count": len(dependents),
    }


def deprecate_test_cases_for_linked_entity(
    entity_type: str,
    base_id: str,
    *,
    updated_by: str = "system",
) -> list[str]:
    """
    Archive current TestCases linked to a deprecated Feature/APIEndpoint.
    Returns affected TestCase base_ids.
    """
    if entity_type not in ("feature", "api_endpoint"):
        return []

    now = _now()
    affected: list[str] = []
    with _get_driver().session() as session:
        linked_values: set[str] = {base_id}

        if entity_type == "feature":
            for row in session.run(
                "MATCH (f:Feature {base_id: $b}) "
                "RETURN DISTINCT f.name AS name, f.archived_name AS archived_name",
                b=base_id,
            ):
                for name in (row.get("name"), row.get("archived_name")):
                    n = (name or "").strip()
                    if n:
                        linked_values.add(n)
        else:
            info = session.run(
                "MATCH (e:APIEndpoint {base_id: $b}) "
                "RETURN e.path AS path, e.method AS method LIMIT 1",
                b=base_id,
            ).single()
            if info:
                path = (info.get("path") or "").strip()
                method = (info.get("method") or "").upper().strip()
                if path:
                    linked_values.add(path)
                if path and method:
                    linked_values.add(f"{method}:{path}")

        for row in session.run(
            "MATCH (t:TestCase {is_current: true}) "
            "WHERE t.linked_to IN $vals "
            "RETURN DISTINCT t.base_id AS base_id",
            vals=list(linked_values),
        ):
            tc_base = row.get("base_id")
            if not tc_base:
                continue
            _expire_current(session, "TestCase", tc_base, now)
            affected.append(tc_base)

    return affected


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

def prune_archived_story_product_edges(story_base_id: str | None = None) -> int:
    """
    Strip product edges from archived UserStory versions.

    Only the live (is_current) story should link features/APIs/test cases; otherwise
    deprecated versions still appear connected after a version upload or restore.
    """
    deleted = 0
    with _get_driver().session() as session:
        params: dict = {"rels": list(_ARCHIVED_STORY_PRODUCT_RELS)}
        if story_base_id:
            params["bid"] = story_base_id
            match_story = (
                "MATCH (s:UserStory {base_id: $bid}) "
                "WHERE coalesce(s.is_current, false) = false"
            )
        else:
            match_story = (
                "MATCH (s:UserStory) WHERE coalesce(s.is_current, false) = false"
            )

        for cypher in (
            f"""
            {match_story}
            MATCH (s)-[r]->()
            WHERE type(r) IN $rels
            DELETE r
            """,
            f"""
            {match_story}
            MATCH ()-[r:HAS_TEST_CASE]->(s)
            DELETE r
            """,
            f"""
            {match_story}
            MATCH ()-[r:BLOCKS]->(s)
            DELETE r
            """,
        ):
            summary = session.run(cypher, **params).consume()
            deleted += summary.counters.relationships_deleted
    return deleted


def prune_edges_on_archived_nodes() -> int:
    """Remove product edges touching archived Feature/API/TestCase (not UserStory versions)."""
    deleted = 0
    with _get_driver().session() as session:
        result = session.run(
            """
            MATCH (a)-[r]->(b)
            WHERE (coalesce(a.is_current, true) = false OR coalesce(b.is_current, true) = false)
              AND NOT a:UserStory AND NOT b:UserStory
            DELETE r
            """
        )
        summary = result.consume()
        deleted += summary.counters.relationships_deleted
    return deleted


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
    """Delete all versions of an entity by base_id."""
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
        "scope": "all_versions",
    }


def delete_node_version(node_id: str) -> dict:
    """
    Delete one version (node_id) only. Other versions with the same base_id remain.

    If the live version is removed and older versions exist, the highest remaining
    version is promoted to live so you can keep working or use ↩ to switch again.
    """
    now = _now()
    with _get_driver().session() as session:
        label = _label_for_node_id(session, node_id)
        if not label or label not in ("UserStory", "Feature", "APIEndpoint", "TestCase"):
            return {"deleted": False, "reason": "not_found"}

        row = session.run(
            f"MATCH (n:{label} {{node_id: $id}}) "
            "RETURN n.base_id AS base_id, n.version AS version, n.is_current AS is_current",
            id=node_id,
        ).single()
        if not row:
            return {"deleted": False, "reason": "not_found"}

        base_id = row["base_id"]
        was_current = bool(row["is_current"])
        deleted_version = row["version"]

        session.run(
            f"MATCH (n {{node_id: $id}})-[r:{REL_PREVIOUS_VERSION}]-() DELETE r",
            id=node_id,
        )
        session.run(
            f"MATCH ()-[r:{REL_PREVIOUS_VERSION}]->(n {{node_id: $id}}) DELETE r",
            id=node_id,
        )
        session.run("MATCH (n {node_id: $id}) DETACH DELETE n", id=node_id)

        remaining = session.run(
            f"MATCH (n:{label} {{base_id: $b}}) RETURN count(n) AS c",
            b=base_id,
        ).single()["c"]

        promoted_node_id = None
        if was_current and remaining > 0:
            next_row = session.run(
                f"MATCH (n:{label} {{base_id: $b}}) "
                "RETURN n.node_id AS id ORDER BY n.version DESC LIMIT 1",
                b=base_id,
            ).single()
            if next_row:
                promoted_node_id = next_row["id"]

    entity_type = LABEL_TO_TYPE.get(label, label.lower())
    result = {
        "deleted": True,
        "node_id": node_id,
        "base_id": base_id,
        "entity_type": entity_type,
        "deleted_version": deleted_version,
        "versions_remaining": remaining,
        "scope": "single_version",
    }

    if promoted_node_id:
        promoted = make_version_current(promoted_node_id)
        result["promoted_to_live"] = promoted_node_id
        result["promoted_version"] = promoted.get("version")
        result["message"] = (
            f"Removed v{deleted_version}; v{promoted.get('version')} is now live "
            f"(older versions kept — use ↩ to switch)"
        )
    elif remaining == 0:
        result["message"] = f"Removed {base_id} (no versions left)"
    else:
        result["message"] = f"Removed v{deleted_version} of {base_id} ({remaining} version(s) kept)"

    return result


def list_current_nodes() -> dict:
    """Inventory of live (is_current) nodes only."""
    inv = list_inventory_nodes()
    live = [n for n in inv["nodes"] if n.get("is_current")]
    by_type: dict[str, int] = {}
    for n in live:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    return {"total": len(live), "by_type": by_type, "nodes": live}


def list_inventory_nodes() -> dict:
    """All KG node versions for the sidebar (live + archived), grouped by base_id."""
    labels = ["UserStory", "Feature", "APIEndpoint", "TestCase"]
    nodes = []
    by_type: dict[str, int] = {}

    with _get_driver().session() as session:
        rows = session.run(
            """
            MATCH (n)
            WHERE any(l IN labels(n) WHERE l IN $labels)
            RETURN labels(n)[0] AS type, n.node_id AS id, n.base_id AS base_id,
                   n.version AS version, n.is_current AS is_current, n.status AS status,
                   properties(n) AS props
            ORDER BY type, n.base_id, n.version DESC
            """,
            labels=labels,
        )
        for row in rows:
            ntype = row["type"]
            props = dict(row["props"] or {})
            by_type[ntype] = by_type.get(ntype, 0) + 1
            is_current = bool(row["is_current"]) if row["is_current"] is not None else False
            nodes.append({
                "id": row["id"],
                "base_id": row["base_id"],
                "type": ntype,
                "entity_type": LABEL_TO_TYPE.get(ntype, ntype.lower()),
                "label": _node_display_label(props),
                "version": row["version"],
                "is_current": is_current,
                "status": row["status"] or ("active" if is_current else "archived"),
            })

    return {"total": len(nodes), "by_type": by_type, "nodes": nodes}


def _label_for_node_id(session, node_id: str) -> str | None:
    row = session.run(
        "MATCH (n {node_id: $id}) RETURN labels(n)[0] AS label LIMIT 1",
        id=node_id,
    ).single()
    return row["label"] if row else None


def make_version_current(node_id: str, *, updated_by: str = "system") -> dict:
    """
    Archive the current live version for this base_id and promote the chosen version to live.

    Does not delete history — switches which version is active (e.g. restore pre-upload flow).
    """
    now = _now()
    with _get_driver().session() as session:
        label = _label_for_node_id(session, node_id)
        if not label or label not in ("UserStory", "Feature", "APIEndpoint", "TestCase"):
            return {"success": False, "reason": "not_found"}

        row = session.run(
            f"MATCH (n:{label} {{node_id: $id}}) "
            "RETURN n.base_id AS base_id, n.is_current AS is_current, n.version AS version",
            id=node_id,
        ).single()
        if not row:
            return {"success": False, "reason": "not_found"}

        base_id = row["base_id"]
        if row["is_current"]:
            return {
                "success": True,
                "already_current": True,
                "node_id": node_id,
                "base_id": base_id,
                "version": row["version"],
                "entity_type": LABEL_TO_TYPE.get(label, label.lower()),
            }

        if _current_version(session, label, base_id) > 0:
            _expire_current(session, label, base_id, now)

        feature_restore = ""
        if label == "Feature":
            feature_restore = ", n.name = coalesce(n.archived_name, n.name), n.archived_name = null"
        elif label == "APIEndpoint":
            feature_restore = ", n.endpoint_id = n.base_id"

        session.run(
            f"MATCH (n:{label} {{node_id: $id}}) "
            f"SET n.is_current = true, n.status = $status, n.valid_to = null, "
            f"n.valid_from = $now, n.updated_at = $now, n.updated_by = $by{feature_restore}",
            id=node_id,
            now=now,
            by=updated_by,
            status=DEFAULT_STATUS,
        )

    entity_type = LABEL_TO_TYPE.get(label, label.lower())
    return {
        "success": True,
        "already_current": False,
        "node_id": node_id,
        "base_id": base_id,
        "version": row["version"],
        "entity_type": entity_type,
        "message": f"{base_id} v{row['version']} is now the live version",
    }


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
            "RETURN tc.node_id AS node_id, tc.base_id AS base_id, tc.title AS title, "
            "tc.type AS type, tc.linked_to AS linked_to",
            id=node_id,
        )]


def get_features_using_api(api_base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (feat:Feature {is_current:true})-[:USES_API]->(ep:APIEndpoint {base_id:$b}) "
            "RETURN DISTINCT feat.node_id AS node_id, feat.base_id AS base_id, feat.name AS name",
            b=api_base_id,
        )]


def get_stories_using_api(api_base_id: str) -> list[dict]:
    with _get_driver().session() as session:
        return [dict(r) for r in session.run(
            "MATCH (s:UserStory {is_current:true})-[:USES_API]->(ep:APIEndpoint {base_id:$b}) "
            "RETURN DISTINCT s.node_id AS node_id, s.base_id AS base_id, s.title AS title",
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
        props.get("archived_name")
        or props.get("title")
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


_GRAPH_LABELS = "['UserStory','Feature','APIEndpoint','TestCase']"

# Product relationships only — never PREVIOUS_VERSION (other story versions are
# viewed by selecting them in the story dropdown, not by walking version edges).
_STORY_SUBGRAPH_RELS = [
    "HAS_FEATURE",
    "USES_API",
    "HAS_TEST_CASE",
    "DEPENDS_ON",
    "DEPENDENCY",
    "VALIDATES_AGAINST",
]

_CYPHER_GRAPH_EDGE_FILTER = f"""
(
  type(r) = 'PREVIOUS_VERSION'
  OR {CYPHER_ACTIVE_EDGE}
  OR (a:UserStory AND coalesce(a.is_current, false) = false)
  OR (b:UserStory AND coalesce(b.is_current, false) = false)
  OR (a:Feature AND coalesce(a.is_current, false) = false)
  OR (b:Feature AND coalesce(b.is_current, false) = false)
)
"""


def _collect_subgraph_node_ids(
    session,
    seed_ids: list[str],
    *,
    flow_feature_ids: set[str] | None = None,
) -> set[str]:
    """
    Collect one story-version snapshot without walking impact-only branches.

    DEPENDENCY edges are useful for impact traversal, but expanding through them here
    makes archived story views absorb unrelated/future dependent test cases.
    """
    ids = {i for i in seed_ids if i}
    feature_ids = set(flow_feature_ids or [])
    ids |= feature_ids

    if not seed_ids:
        return ids

    story_node_id = seed_ids[0]

    for row in session.run(
        """
        MATCH (:UserStory {node_id:$story})-[:USES_API]->(api:APIEndpoint)
        RETURN api.node_id AS id
        UNION
        MATCH (f:Feature)-[:USES_API]->(api:APIEndpoint)
        WHERE f.node_id IN $feature_ids
        RETURN api.node_id AS id
        """,
        story=story_node_id,
        feature_ids=list(feature_ids),
    ):
        if row["id"]:
            ids.add(row["id"])

    for row in session.run(
        """
        MATCH (owner)-[:HAS_TEST_CASE]->(tc:TestCase)
        WHERE owner.node_id IN $owner_ids
        RETURN DISTINCT tc.node_id AS id
        """,
        owner_ids=list(ids),
    ):
        if row["id"]:
            ids.add(row["id"])

    return ids


def get_story_subgraph(story_node_id: str) -> dict:
    """Only nodes and edges for one UserStory version's flows[] (removed features = standalone)."""
    from services.linking_engine import (
        _feature_depends_pairs,
        _flow_depends_pairs,
        _flow_feature_node_ids,
    )
    from services.story_flow_delta import compute_story_flow_delta

    story_version = get_user_story_version(story_node_id)
    flows = list((story_version or {}).get("flows") or [])
    flow_feature_ids = _flow_feature_node_ids(flows)

    nodes, edges = [], []
    seen_n, seen_e = set(), set()

    with _get_driver().session() as session:
        seed = session.run(
            "MATCH (n:UserStory {node_id: $id}) RETURN n.node_id AS id LIMIT 1",
            id=story_node_id,
        ).single()
        if not seed:
            return {"nodes": [], "edges": [], "story_id": None, "scoped_to_story": True}

        node_ids = _collect_subgraph_node_ids(
            session, [story_node_id], flow_feature_ids=flow_feature_ids
        )

        rows = session.run(
            f"""
            MATCH (n)
            WHERE n.node_id IN $ids
              AND any(l IN labels(n) WHERE l IN {_GRAPH_LABELS})
            RETURN labels(n)[0] AS type, n.node_id AS id, n.base_id AS base_id,
                   n.version AS version, n.is_current AS is_current, n.status AS status,
                   properties(n) AS props
            """,
            ids=list(node_ids),
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
                "is_current": bool(row["is_current"]) if row["is_current"] is not None else True,
                "status": row["status"],
                "properties": _serialize_props(props),
            })

        flow_depends = _flow_depends_pairs(flows)

        if seen_n:
            for row in session.run(
                f"""
                MATCH (a)-[r]->(b)
                WHERE a.node_id IN $ids AND b.node_id IN $ids
                  AND type(r) IN $rels
                RETURN a.node_id AS source, b.node_id AS target, type(r) AS rel_type,
                       labels(a)[0] AS sa, labels(b)[0] AS sb
                """,
                ids=list(seen_n),
                rels=_STORY_SUBGRAPH_RELS,
            ):
                src, tgt, rel = row["source"], row["target"], row["rel_type"]
                if rel == "DEPENDS_ON" and row["sa"] == "Feature" and row["sb"] == "Feature":
                    if src not in flow_feature_ids or tgt not in flow_feature_ids:
                        continue
                    if (src, tgt) not in flow_depends:
                        continue
                if rel == "HAS_FEATURE" and row["sa"] == "UserStory":
                    if tgt not in flow_feature_ids:
                        continue
                key = f"{src}|{rel}|{tgt}"
                if key in seen_e:
                    continue
                seen_e.add(key)
                edges.append({
                    "id": key,
                    "source": src,
                    "target": tgt,
                    "rel_type": rel,
                })

        story_base = session.run(
            "MATCH (n:UserStory {node_id: $id}) RETURN n.base_id AS b",
            id=story_node_id,
        ).single()

        story_base_id = story_base["b"] if story_base else None
        delta = None
        if story_base_id:
            delta = compute_story_flow_delta(story_base_id, story_node_id=story_node_id)
            for ref in delta.get("removed") or []:
                nid = ref.get("node_id")
                if not nid or nid in seen_n:
                    continue
                props = get_node_props(nid)
                if not props:
                    continue
                seen_n.add(nid)
                nodes.append({
                    "id": nid,
                    "base_id": props.get("base_id"),
                    "type": "Feature",
                    "entity_type": "feature",
                    "label": _node_display_label(props),
                    "version": props.get("version"),
                    "is_current": bool(props.get("is_current", True)),
                    "status": props.get("status"),
                    "properties": _serialize_props(props),
                    "orphan_removed": True,
                })

    return {
        "nodes": nodes,
        "edges": edges,
        "story_id": story_base_id,
        "focus_story_node_id": story_node_id,
        "scoped_to_story": True,
        "story_flow_delta": delta,
    }


def _prepare_full_graph_flow_context(session) -> tuple[dict, set[str], set[tuple[str, str]]]:
    """
    Build per-story-version flow membership and live-story DEPENDS_ON pairs.

    Used by All nodes view: live v2 has no Payment links; v1 archived may still show Payment.
    """
    from services.linking_engine import (
        _feature_depends_pairs,
        _flow_depends_pairs,
        _flow_feature_node_ids,
    )

    flows_by_story: dict[str, set[str]] = {}
    live_feature_ids: set[str] = set()
    live_flow_depends: set[tuple[str, str]] = set()

    for row in session.run(
        "MATCH (s:UserStory) "
        "RETURN s.node_id AS id, s.flows AS flows, coalesce(s.is_current, false) AS is_current"
    ):
        story_nid = row["id"]
        flows = list(row["flows"] or [])
        fids = _flow_feature_node_ids(flows)
        flows_by_story[story_nid] = fids
        if row["is_current"]:
            live_feature_ids |= fids
            live_flow_depends |= _flow_depends_pairs(flows) | _feature_depends_pairs(flows)

    return flows_by_story, live_feature_ids, live_flow_depends


def _full_graph_edge_allowed(
    row: dict,
    *,
    flows_by_story: dict[str, set[str]],
    live_feature_ids: set[str],
    live_flow_depends: set[tuple[str, str]],
) -> bool:
    src, tgt, rel = row["source"], row["target"], row["rel_type"]
    sa, sb = row.get("sa"), row.get("sb")

    if rel == "HAS_FEATURE" and sa == "UserStory":
        return tgt in flows_by_story.get(src, set())

    if rel == "DEPENDS_ON" and sa == "Feature" and sb == "Feature":
        return (src, tgt) in live_flow_depends

    return True


def get_full_graph(story_base_id: str | None = None) -> dict:
    """
    Export all KG versions. Flow edges follow each story version's flows[];
    Feature DEPENDS_ON (dependent→prerequisite) follows the live story flows[] only (e.g. v2 without Payment).
    """
    from services.story_flow_delta import compute_story_flow_delta

    nodes, edges = [], []
    seen_n, seen_e = set(), set()

    with _get_driver().session() as session:
        flows_by_story, live_feature_ids, live_flow_depends = _prepare_full_graph_flow_context(session)

        rows = session.run(
            f"""
            MATCH (n)
            WHERE any(l IN labels(n) WHERE l IN {_GRAPH_LABELS})
            RETURN labels(n)[0] AS type, n.node_id AS id, n.base_id AS base_id,
                   n.version AS version, n.is_current AS is_current, n.status AS status,
                   properties(n) AS props
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
                "is_current": bool(row["is_current"]) if row["is_current"] is not None else True,
                "status": row["status"],
                "properties": _serialize_props(props),
            })

        if seen_n:
            for row in session.run(
                f"""
                MATCH (a)-[r]->(b)
                WHERE a.node_id IN $ids AND b.node_id IN $ids
                  AND {_CYPHER_GRAPH_EDGE_FILTER}
                RETURN a.node_id AS source, b.node_id AS target, type(r) AS rel_type,
                       labels(a)[0] AS sa, labels(b)[0] AS sb
                """,
                ids=list(seen_n),
            ):
                if not _full_graph_edge_allowed(
                    row,
                    flows_by_story=flows_by_story,
                    live_feature_ids=live_feature_ids,
                    live_flow_depends=live_flow_depends,
                ):
                    continue
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

    ref_base = story_base_id
    if not ref_base:
        live_stories = get_all_user_stories()
        if len(live_stories) == 1:
            ref_base = live_stories[0]["base_id"]

    live_story = get_user_story(ref_base) if ref_base else None
    story_flow_delta = None
    if live_story and ref_base:
        story_flow_delta = compute_story_flow_delta(
            ref_base, story_node_id=live_story["node_id"]
        )
        for ref in story_flow_delta.get("removed") or []:
            nid = ref.get("node_id")
            if not nid:
                continue
            for n in nodes:
                if n["id"] == nid:
                    n["orphan_removed"] = True
                    break

    return {
        "nodes": nodes,
        "edges": edges,
        "story_id": ref_base or story_base_id,
        "focus_story_node_id": live_story["node_id"] if live_story else None,
        "scoped_to_story": False,
        "story_flow_delta": story_flow_delta,
    }


def check_connection() -> dict:
    try:
        with _get_driver().session() as session:
            r = session.run("RETURN 1 AS ok").single()
            return {"connected": bool(r and r["ok"] == 1)}
    except Exception as e:
        return {"connected": False, "error": str(e)}
