-- =================================================================
-- Migration 012: Add tournament log entries table
-- =================================================================
-- Date: 2026-08-23
-- Description: Creates lan_tournament_log_entries for audit log
--   storage (tournament engine and bracket consistency).
--
-- Shape mirrors byceps/services/tourney/log/dbmodels.py:
--   - UUID primary key (application-generated uuid7)
--   - timestamptz occurred_at
--   - event_type text discriminator, payload in JSONB data
--   - FK to lan_tournaments.id (indexed), nullable FK to users.id
--
-- BYCEPS convention: NO CASCADE behaviors. Log entries are removed
-- explicitly at the application service layer.
--
-- Idempotent: IF NOT EXISTS on table and index.
-- Transaction-wrapped.
-- Rollback: rollback_012.sql
-- =================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS lan_tournament_log_entries (
    id UUID NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    event_type TEXT NOT NULL,
    tournament_id UUID NOT NULL,
    initiator_id UUID NULL,
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT pk_lan_tournament_log_entries PRIMARY KEY (id),
    CONSTRAINT fk_lan_tournament_log_entries_tournament_id
        FOREIGN KEY (tournament_id) REFERENCES lan_tournaments (id),
    CONSTRAINT fk_lan_tournament_log_entries_initiator_id
        FOREIGN KEY (initiator_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS ix_lan_tournament_log_entries_tournament_id
    ON lan_tournament_log_entries (tournament_id);

COMMIT;
