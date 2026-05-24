"""
schema_service.py  —  Engineer 1
Creates Neo4j constraints and indexes for all 5 node types.
Run once: python main.py setup-schema
"""

from services import graph_service


def setup() -> None:
    constraints = [
        ("UserStory",   "node_id"),
        ("Feature",     "node_id"),
        ("APIEndpoint", "node_id"),
        ("Flow",        "node_id"),
        ("TestCase",    "node_id"),
    ]

    indexes = [
        ("UserStory",   "base_id"),
        ("Feature",     "base_id"),
        ("Feature",     "name"),
        ("APIEndpoint", "base_id"),
        ("APIEndpoint", "path"),
        ("Flow",        "base_id"),
        ("Flow",        "story_id"),
        ("TestCase",    "base_id"),
        ("TestCase",    "flow_id"),
    ]

    driver = graph_service._get_driver()

    with driver.session() as session:
        for label, prop in constraints:
            try:
                session.run(
                    f"CREATE CONSTRAINT IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
                )
                print(f"  ✓ CONSTRAINT  ({label}).{prop}  IS UNIQUE")
            except Exception as e:
                print(f"  [skip] ({label}).{prop} — {e}")

        for label, prop in indexes:
            try:
                session.run(
                    f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.{prop})"
                )
                print(f"  ✓ INDEX       ({label}).{prop}")
            except Exception as e:
                print(f"  [skip] ({label}).{prop} — {e}")

    print("\n  Schema setup complete.")
