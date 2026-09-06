-- migrate_audit_events.sql
--
-- Migration: Create audit_events table for system-wide audit logging.
-- Story: STORY-3 — audit_events database migration
--
-- Conventions:
--   * Idempotent: all CREATE statements use IF NOT EXISTS / IF NOT EXISTS,
--     so the script is safe to re-run on an already-migrated database.
--   * Runs inside a single transaction; the shell wrapper (run_migration.sh)
--     passes -v ON_ERROR_STOP=1 and --single-transaction to psql.
--   * No psql meta-commands (\echo, \i, etc.) — this file is executable
--     by both the psql CLI and by psycopg/sqlalchemy-style Python callers.
--
-- Schema:
--   audit_events(
--       id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
--       event_type    VARCHAR(255) NOT NULL,
--       timestamp     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
--       actor         JSONB NOT NULL DEFAULT '{}',
--       component     VARCHAR(255),
--       resource      JSONB,
--       action        VARCHAR(255),
--       outcome       VARCHAR(50) DEFAULT 'success',
--       metadata      JSONB NOT NULL DEFAULT '{}',
--       raw_detail    JSONB
--   )
--
-- Indexes (5 total):
--   idx_audit_events_timestamp   ON audit_events (timestamp DESC)
--   idx_audit_events_event_type ON audit_events (event_type)
--   idx_audit_events_actor      ON audit_events USING GIN (actor)
--   idx_audit_events_component  ON audit_events (component)
--   idx_audit_events_resource   ON audit_events USING GIN (resource)

-- ---------------------------------------------------------------------------
-- (1) Create audit_events table (idempotent via IF NOT EXISTS)

CREATE TABLE IF NOT EXISTS audit_events (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type    VARCHAR(255) NOT NULL,
    timestamp     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor         JSONB NOT NULL DEFAULT '{}',
    component     VARCHAR(255),
    resource      JSONB,
    action        VARCHAR(255),
    outcome       VARCHAR(50) DEFAULT 'success',
    metadata      JSONB NOT NULL DEFAULT '{}',
    raw_detail    JSONB
);

-- ---------------------------------------------------------------------------
-- (2) Create indexes (idempotent via IF NOT EXISTS)

CREATE INDEX IF NOT EXISTS idx_audit_events_timestamp
    ON audit_events (timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_event_type
    ON audit_events (event_type);

CREATE INDEX IF NOT EXISTS idx_audit_events_actor
    ON audit_events USING GIN (actor);

CREATE INDEX IF NOT EXISTS idx_audit_events_component
    ON audit_events (component);

CREATE INDEX IF NOT EXISTS idx_audit_events_resource
    ON audit_events USING GIN (resource);

-- ---------------------------------------------------------------------------
-- (3) Record this migration in schema_migrations (idempotent via UNIQUE constraint)

INSERT INTO schema_migrations (migration_name, applied_at)
VALUES ('audit_events_v1', NOW())
ON CONFLICT (migration_name) DO NOTHING;
