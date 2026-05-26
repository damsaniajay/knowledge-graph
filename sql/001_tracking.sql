-- PostgreSQL tracking tables (doc-aligned history + audit).
-- Neo4j remains the live graph; these tables are the relational audit trail.

CREATE TABLE IF NOT EXISTS entity_history (
    id              BIGSERIAL PRIMARY KEY,
    entity_type     VARCHAR(32)  NOT NULL,
    base_id         VARCHAR(256) NOT NULL,
    node_id         VARCHAR(256) NOT NULL,
    version         INTEGER      NOT NULL,
    status          VARCHAR(32)  NOT NULL DEFAULT 'active',
    is_current      BOOLEAN      NOT NULL DEFAULT true,
    valid_from      TIMESTAMPTZ  NOT NULL,
    valid_to        TIMESTAMPTZ,
    content_hash    VARCHAR(64),
    version_policy  VARCHAR(16),
    payload         JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_by      VARCHAR(128) NOT NULL DEFAULT 'system',
    UNIQUE (entity_type, node_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_history_base
    ON entity_history (entity_type, base_id, version DESC);

CREATE INDEX IF NOT EXISTS idx_entity_history_current
    ON entity_history (entity_type, base_id) WHERE is_current = true;

CREATE TABLE IF NOT EXISTS upload_events (
    id              BIGSERIAL PRIMARY KEY,
    entity_type     VARCHAR(32)  NOT NULL,
    base_id         VARCHAR(256),
    node_id         VARCHAR(256),
    version         INTEGER,
    filename        VARCHAR(512),
    version_policy  VARCHAR(16),
    is_new_entity   BOOLEAN,
    identity_source VARCHAR(32),
    delta_summary   TEXT,
    content_hash    VARCHAR(64),
    extra           JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_upload_events_base
    ON upload_events (entity_type, base_id, created_at DESC);

CREATE TABLE IF NOT EXISTS delta_events (
    id              BIGSERIAL PRIMARY KEY,
    entity_type     VARCHAR(32)  NOT NULL,
    base_id         VARCHAR(256) NOT NULL,
    from_node_id    VARCHAR(256),
    to_node_id      VARCHAR(256) NOT NULL,
    from_version    INTEGER,
    to_version      INTEGER      NOT NULL,
    delta_source    VARCHAR(32)  NOT NULL DEFAULT 'llm',
    delta_summary   TEXT,
    delta_detail    JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_delta_events_base
    ON delta_events (entity_type, base_id, created_at DESC);

CREATE TABLE IF NOT EXISTS edge_history (
    id              BIGSERIAL PRIMARY KEY,
    rel_type        VARCHAR(64)  NOT NULL,
    source_node_id  VARCHAR(256) NOT NULL,
    target_node_id  VARCHAR(256) NOT NULL,
    status          VARCHAR(32)  NOT NULL DEFAULT 'active',
    valid_from      TIMESTAMPTZ  NOT NULL,
    valid_to        TIMESTAMPTZ,
    properties      JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_edge_history_nodes
    ON edge_history (source_node_id, target_node_id, rel_type);
