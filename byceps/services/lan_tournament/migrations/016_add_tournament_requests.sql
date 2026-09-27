-- =================================================================
-- Migration 016: Add tournament requests table
-- =================================================================
-- Date: 2026-09-27
-- Description: Creates lan_tournament_requests, the user-facing
--   propose-a-tournament queue (PRD §22), and adds
--   lan_tournaments.created_from_request_id linking an accepted
--   request to the tournament it produced.
--
-- Shape notes:
--   - UUID primary key (application-generated uuid7)
--   - FK to parties.id (indexed) and FK to users.id for the
--     proposer (indexed) and the decider (nullable, unindexed)
--   - UNIQUE (party_id, number) backs the sequential per-party
--     request number (e.g. "#0142"), allocated as MAX(number)+1
--   - status TEXT NOT NULL, indexed for the admin queue filter
--   - ck_lan_tournament_requests_rejection_reason requires a
--     rejection_reason once status = 'rejected'
--   - created_tournament_id is a nullable FK to lan_tournaments.id,
--     set once the request is accepted and a tournament created
--     from it, and backed by a partial index (WHERE NOT NULL) for
--     the unlink-on-delete lookup and the FK check on tournament
--     deletion
--   - lan_tournaments.created_from_request_id is the inverse link,
--     guarded by a partial UNIQUE index so a second create from
--     the same request cannot produce a second tournament
--     (PRD §26 idempotency guard)
--
-- BYCEPS convention: NO CASCADE behaviors. Requests and their
-- linkage are removed explicitly at the application service layer.
--
-- Idempotent: IF NOT EXISTS on table, both altered columns and all
-- indexes.
-- Transaction-wrapped.
-- Rollback: rollback_016.sql
-- =================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS lan_tournament_requests (
    id UUID NOT NULL,
    party_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    proposer_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NULL,
    status TEXT NOT NULL,
    name TEXT NOT NULL,
    game TEXT NOT NULL,
    game_format TEXT NOT NULL,
    elimination_mode TEXT NOT NULL,
    team_size INTEGER NOT NULL,
    participant_limit INTEGER NOT NULL,
    preferred_start_time TIMESTAMPTZ NOT NULL,
    preferred_end_time TIMESTAMPTZ NOT NULL,
    description TEXT NOT NULL,
    special_rules TEXT NULL,
    notes TEXT NULL,
    desired_template TEXT NULL,
    decided_at TIMESTAMPTZ NULL,
    decided_by_id UUID NULL,
    rejection_reason TEXT NULL,
    created_tournament_id UUID NULL,
    CONSTRAINT pk_lan_tournament_requests PRIMARY KEY (id),
    CONSTRAINT uq_lan_tournament_requests_party_number UNIQUE (party_id, number),
    CONSTRAINT fk_lan_tournament_requests_party_id
        FOREIGN KEY (party_id) REFERENCES parties (id),
    CONSTRAINT fk_lan_tournament_requests_proposer_id
        FOREIGN KEY (proposer_id) REFERENCES users (id),
    CONSTRAINT fk_lan_tournament_requests_decided_by_id
        FOREIGN KEY (decided_by_id) REFERENCES users (id),
    CONSTRAINT fk_lan_tournament_requests_created_tournament_id
        FOREIGN KEY (created_tournament_id) REFERENCES lan_tournaments (id),
    CONSTRAINT ck_lan_tournament_requests_period
        CHECK (preferred_end_time >= preferred_start_time),
    CONSTRAINT ck_lan_tournament_requests_rejection_reason
        CHECK (status <> 'rejected' OR rejection_reason IS NOT NULL),
    CONSTRAINT ck_lan_tournament_requests_number CHECK (number > 0),
    CONSTRAINT ck_lan_tournament_requests_team_size CHECK (team_size >= 1),
    CONSTRAINT ck_lan_tournament_requests_limit CHECK (participant_limit >= 2)
);

CREATE INDEX IF NOT EXISTS ix_lan_tournament_requests_party_id
    ON lan_tournament_requests (party_id);
CREATE INDEX IF NOT EXISTS ix_lan_tournament_requests_proposer_id
    ON lan_tournament_requests (proposer_id);
CREATE INDEX IF NOT EXISTS ix_lan_tournament_requests_status
    ON lan_tournament_requests (status);

-- Partial: most requests never produce a tournament. Backs
-- unlink_created_tournament_flush's lookup by created_tournament_id
-- (delete_tournament cascade) and the created_tournament_id FK check
-- that fires on every lan_tournaments DELETE.
CREATE INDEX IF NOT EXISTS ix_lan_tournament_requests_created_tournament_id
    ON lan_tournament_requests (created_tournament_id)
    WHERE created_tournament_id IS NOT NULL;

ALTER TABLE lan_tournaments
    ADD COLUMN IF NOT EXISTS created_from_request_id UUID NULL;

-- Partial: most tournaments were never created from a request, and only
-- rows that were need to be unique on this column (PRD §26 idempotency
-- guard -- a second create from the same request cannot produce a second
-- tournament).
CREATE UNIQUE INDEX IF NOT EXISTS uq_lan_tournaments_created_from_request_id
    ON lan_tournaments (created_from_request_id)
    WHERE created_from_request_id IS NOT NULL;

COMMIT;
