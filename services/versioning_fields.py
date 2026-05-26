"""
Temporal versioning field names — aligned with KnowledgeGraph_Schema (3).docx.

Doc review (Aravinda PR) on rows labelled valid_at / invalid_at was:
  "Status still is missing. Can that be brought in."
→ Add lifecycle `status`, not remove timeline fields.

Canonical names (history tables + graph nodes):
  valid_from  — when this version became active
  valid_to    — when superseded (null = still active)

Legacy Neo4j writes used valid_at / invalid_at; reads coalesce both.
"""

# Cypher fragments (nodes)
CYPHER_ACTIVE_EDGE = (
    "coalesce(r.valid_to, r.invalid_at) IS NULL "
    "AND coalesce(a.is_current, true) = true AND coalesce(b.is_current, true) = true"
)

CYPHER_EXPIRE_NODE_SET = """
n.is_current = false,
n.status = 'archived',
n.valid_to = $now,
n.updated_at = $now,
n.updated_by = coalesce(n.updated_by, n.created_by)
"""

CYPHER_CREATE_TEMPORAL = """
valid_from: $now,
valid_to: null,
status: $status,
"""

CYPHER_EDGE_CREATE_TEMPORAL = """
r.valid_from = $now,
r.valid_to = null,
r.status = $status,
"""

# History SELECT (returns canonical names)
CYPHER_HISTORY_VALID = """
coalesce(n.valid_from, n.valid_at) AS valid_from,
coalesce(n.valid_to, n.invalid_at) AS valid_to
"""
