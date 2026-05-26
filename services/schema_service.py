"""Neo4j constraints and indexes — schema v2."""

from services import graph_service


def _drop_legacy_endpoint_id_unique(session) -> None:
    """
    Older setups created UNIQUE(APIEndpoint.endpoint_id). That breaks versioning
  (v2 needs the same METHOD:path on a new node). Only node_id must be unique.
    """
    try:
        rows = list(session.run("SHOW CONSTRAINTS"))
    except Exception as e:
        print(f"  [skip] SHOW CONSTRAINTS — {e}")
        return

    for row in rows:
        data = dict(row)
        name = data.get("name")
        labels = data.get("labelsOrTypes") or data.get("entityType") or []
        props = data.get("properties") or data.get("propertyNames") or []
        if isinstance(labels, str):
            labels = [labels]
        if "APIEndpoint" not in labels:
            continue
        if props != ["endpoint_id"]:
            continue
        try:
            session.run(f"DROP CONSTRAINT `{name}` IF EXISTS")
            print(f"  ✓ DROPPED legacy CONSTRAINT ({name}) on APIEndpoint.endpoint_id")
        except Exception as e:
            print(f"  [warn] could not drop {name}: {e}")


def setup() -> None:
    constraints = [
        ("UserStory", "node_id"),
        ("Feature", "node_id"),
        ("APIEndpoint", "node_id"),
        ("APIResponseSchema", "node_id"),
        ("TestCase", "node_id"),
    ]

    indexes = [
        ("UserStory", "base_id"),
        ("Feature", "base_id"),
        ("Feature", "name"),
        ("APIEndpoint", "base_id"),
        ("APIEndpoint", "endpoint_id"),
        ("APIEndpoint", "path"),
        ("APIResponseSchema", "endpoint_id"),
        ("TestCase", "base_id"),
        ("TestCase", "linked_to"),
        ("UserStory", "content_hash"),
        ("Feature", "content_hash"),
        ("APIEndpoint", "content_hash"),
        ("APIEndpoint", "openapi_bundle_hash"),
        ("TestCase", "content_hash"),
    ]

    driver = graph_service._get_driver()
    with driver.session() as session:
        _drop_legacy_endpoint_id_unique(session)

        for label, prop in constraints:
            try:
                session.run(
                    f"CREATE CONSTRAINT IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
                )
                print(f"  ✓ CONSTRAINT  ({label}).{prop}")
            except Exception as e:
                print(f"  [skip] ({label}).{prop} — {e}")

        for label, prop in indexes:
            try:
                session.run(f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.{prop})")
                print(f"  ✓ INDEX       ({label}).{prop}")
            except Exception as e:
                print(f"  [skip] ({label}).{prop} — {e}")

    repair = repair_endpoint_id_collisions()
    if repair.get("archived_endpoints_repaired"):
        print(f"  ✓ Repaired {repair['archived_endpoints_repaired']} archived endpoint(s)")

    temporal = migrate_temporal_property_names()
    if temporal.get("nodes_migrated") or temporal.get("relationships_migrated"):
        print(
            f"  ✓ Temporal fields: {temporal.get('nodes_migrated', 0)} node(s), "
            f"{temporal.get('relationships_migrated', 0)} rel(s) → valid_from / valid_to"
        )

    print("\n  Schema setup complete.")
    print("  Note: API spec v2 re-upload versions endpoints by base_id (METHOD:path), not duplicate insert.")


def repair_endpoint_id_collisions() -> dict:
    """
    One-time fix: archived APIEndpoint nodes still holding METHOD:path in endpoint_id
    under a legacy UNIQUE constraint. Run via setup-schema or POST /api/graph/repair-schema.
    """
    driver = graph_service._get_driver()
    fixed = 0
    with driver.session() as session:
        _drop_legacy_endpoint_id_unique(session)
        result = session.run(
            """
            MATCH (n:APIEndpoint)
            WHERE n.is_current = false AND n.endpoint_id = n.base_id
            SET n.endpoint_id = n.node_id
            RETURN count(n) AS c
            """
        ).single()
        fixed = int(result["c"]) if result else 0
    temporal = migrate_temporal_property_names()
    return {
        "archived_endpoints_repaired": fixed,
        **temporal,
    }


def migrate_temporal_property_names() -> dict:
    """
    Align Neo4j with docx: valid_from / valid_to (drop legacy valid_at / invalid_at).
    Safe to run multiple times.
    """
    driver = graph_service._get_driver()
    nodes = rels = 0
    with driver.session() as session:
        r = session.run(
            """
            MATCH (n)
            WHERE (n.valid_at IS NOT NULL OR n.invalid_at IS NOT NULL)
              AND any(l IN labels(n) WHERE l IN
                ['UserStory','Feature','APIEndpoint','APIResponseSchema','TestCase'])
            SET n.valid_from = coalesce(n.valid_from, n.valid_at),
                n.valid_to = coalesce(n.valid_to, n.invalid_at)
            REMOVE n.valid_at, n.invalid_at
            RETURN count(n) AS c
            """
        ).single()
        nodes = int(r["c"]) if r else 0

        r2 = session.run(
            """
            MATCH ()-[r]->()
            WHERE r.valid_at IS NOT NULL OR r.invalid_at IS NOT NULL
            SET r.valid_from = coalesce(r.valid_from, r.valid_at),
                r.valid_to = coalesce(r.valid_to, r.invalid_at)
            REMOVE r.valid_at, r.invalid_at
            RETURN count(r) AS c
            """
        ).single()
        rels = int(r2["c"]) if r2 else 0

    return {"nodes_migrated": nodes, "relationships_migrated": rels}
