-- =================================================================
-- Migration 014: Add tournament orga assignment table
-- =================================================================
-- Date: 2026-09-19
-- Description: Creates lan_tournament_orgas, recording which users
--   are assigned as organizers ("orgas") of a given tournament.
--
-- Shape mirrors byceps/services/orga_team/dbmodels.py (DbMembership):
--   - UUID primary key (application-generated uuid7)
--   - FK to lan_tournaments.id (indexed) and FK to users.id (indexed)
--   - UNIQUE (tournament_id, user_id) guards against double-assignment,
--     including a double-submit of the assign form
--   - timestamptz assigned_at, nullable FK assigned_by_id (system- or
--     fixture-created assignments have no initiator), optional duties
--
-- BYCEPS convention: NO CASCADE behaviors. Orga assignments are
-- removed explicitly at the application service layer.
--
-- Idempotent: IF NOT EXISTS on table and both indexes.
-- Transaction-wrapped.
-- Rollback: rollback_014.sql
-- =================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS lan_tournament_orgas (
    id UUID NOT NULL,
    tournament_id UUID NOT NULL,
    user_id UUID NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL,
    assigned_by_id UUID NULL,
    duties TEXT NULL,
    CONSTRAINT pk_lan_tournament_orgas PRIMARY KEY (id),
    CONSTRAINT uq_lan_tournament_orgas_tournament_user
        UNIQUE (tournament_id, user_id),
    CONSTRAINT fk_lan_tournament_orgas_tournament_id
        FOREIGN KEY (tournament_id) REFERENCES lan_tournaments (id),
    CONSTRAINT fk_lan_tournament_orgas_user_id
        FOREIGN KEY (user_id) REFERENCES users (id),
    CONSTRAINT fk_lan_tournament_orgas_assigned_by_id
        FOREIGN KEY (assigned_by_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS ix_lan_tournament_orgas_tournament_id
    ON lan_tournament_orgas (tournament_id);
CREATE INDEX IF NOT EXISTS ix_lan_tournament_orgas_user_id
    ON lan_tournament_orgas (user_id);

COMMIT;
