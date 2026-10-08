-- =================================================================
-- Migration 023: Operational timing and dashboard coordination history
-- =================================================================
-- Date: 2026-10-07
-- Description: Adds the persistent facts of the cross-tournament orga
--   dashboard (PRD F-03): the operational clock of a tournament, the
--   domain-only last-change timestamp of a match and four tables for
--   due episodes, escalation acknowledgements, shared pins and the
--   per-party traffic thresholds.
--
-- Shape notes:
--   - lan_tournaments.operational_clock_elapsed_us is BIGINT NOT NULL
--     DEFAULT 0 (microseconds of active time), checked >= 0 by
--     ck_lan_tournaments_operational_clock_elapsed_us.
--     operational_clock_running_since and operational_clock_activated_at
--     are nullable naive TIMESTAMPs. Every microsecond column of this
--     migration is BIGINT: an INTEGER overflows after 35 min 47 s
--   - lan_tournament_matches.last_changed_at is a nullable naive
--     TIMESTAMP. Existing rows keep NULL, which means unknown history:
--     nothing is backfilled and no clock is invented for tournaments
--     that already started
--   - lan_tournament_match_due_episodes: one row per uninterrupted
--     period of due demand of a match. At most one open episode per
--     match (uq_lan_tournament_due_episodes_open_match, a partial
--     unique index); closed_at and closed_clock_us are both NULL or
--     both set. Two more partial indexes keep the hot reads off a
--     sequential scan of the retained history:
--     ix_lan_tournament_due_episodes_open_tournament (tournament_id,
--     open episodes: the lookup inside every result write) and
--     ix_lan_tournament_due_episodes_closed_match (match_id, closed
--     episodes: the per-fixture dashboard facts)
--   - lan_tournament_match_escalation_acks: one row per acknowledged
--     episode revision (uq_lan_tournament_escalation_ack_episode_revision).
--     The foreign key to the episode table is the only foreign key of
--     the new tables
--   - lan_tournament_match_dashboard_annotations: the shared pin of a
--     live match, keyed by the match ID; pinned_at and pinned_by are
--     both NULL or both set
--   - lan_tournament_dashboard_party_thresholds: the Wartung override
--     of the traffic thresholds of one party, keyed by the party ID.
--     No row means the deployment default applies
--   - tournament, match, party and actor IDs of the new tables are
--     snapshots WITHOUT a foreign key, so history survives the deletion
--     or regeneration of the live rows. The application removes pin
--     rows together with their match; episodes, acknowledgements and
--     thresholds are retained
--   - timestamps are naive TIMESTAMP, as in the dbmodels
--   - no index is added for unconfirmed match frontiers or active team
--     memberships: no query needs one yet
--
-- BYCEPS convention: NO CASCADE behaviors.
--
-- Idempotent: IF NOT EXISTS on columns, tables and indexes;
-- PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS, so the added
-- constraint is guarded by a pg_constraint lookup that is scoped to
-- its relation. Transaction-wrapped.
-- Rollback: rollback_023.sql
-- =================================================================

BEGIN;
SET LOCAL lock_timeout = '5s';

-- Tournament first, then matches: the order of the application's writers.
ALTER TABLE lan_tournaments
    ADD COLUMN IF NOT EXISTS operational_clock_elapsed_us BIGINT
        NOT NULL DEFAULT '0',
    ADD COLUMN IF NOT EXISTS operational_clock_running_since TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS operational_clock_activated_at TIMESTAMP NULL;

ALTER TABLE lan_tournament_matches
    ADD COLUMN IF NOT EXISTS last_changed_at TIMESTAMP NULL;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = to_regclass('lan_tournaments')
          AND conname = 'ck_lan_tournaments_operational_clock_elapsed_us'
    ) THEN
        ALTER TABLE lan_tournaments
            ADD CONSTRAINT ck_lan_tournaments_operational_clock_elapsed_us
            CHECK (operational_clock_elapsed_us >= 0);
    END IF;
END;
$migration$;

CREATE TABLE IF NOT EXISTS lan_tournament_match_due_episodes (
    id UUID NOT NULL,
    tournament_id UUID NOT NULL,
    match_id UUID NOT NULL,
    pairing_key TEXT NOT NULL,
    opened_at TIMESTAMP NOT NULL,
    opened_clock_us BIGINT NOT NULL,
    closed_at TIMESTAMP NULL,
    closed_clock_us BIGINT NULL,
    ack_revision INTEGER NOT NULL DEFAULT '0',
    PRIMARY KEY (id),
    CONSTRAINT ck_lan_tournament_due_episodes_opened_clock_us
        CHECK (opened_clock_us >= 0),
    CONSTRAINT ck_lan_tournament_due_episodes_closed_clock_us
        CHECK (closed_clock_us >= 0),
    CONSTRAINT ck_lan_tournament_due_episodes_ack_revision
        CHECK (ack_revision >= 0),
    CONSTRAINT ck_lan_tournament_due_episodes_close_pair
        CHECK ((closed_at IS NULL AND closed_clock_us IS NULL)
            OR (closed_at IS NOT NULL AND closed_clock_us IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_lan_tournament_due_episodes_open_match
    ON lan_tournament_match_due_episodes (match_id)
    WHERE closed_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_lan_tournament_due_episodes_open_tournament
    ON lan_tournament_match_due_episodes (tournament_id)
    WHERE closed_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_lan_tournament_due_episodes_closed_match
    ON lan_tournament_match_due_episodes (match_id)
    WHERE closed_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS lan_tournament_match_escalation_acks (
    id UUID NOT NULL,
    episode_id UUID NOT NULL,
    tournament_id UUID NOT NULL,
    match_id UUID NOT NULL,
    actor_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    occurred_at TIMESTAMP NOT NULL,
    clock_us BIGINT NOT NULL,
    comment VARCHAR(500) NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_lan_tournament_escalation_ack_episode_revision
        UNIQUE (episode_id, revision),
    CONSTRAINT ck_lan_tournament_escalation_ack_revision
        CHECK (revision >= 1),
    CONSTRAINT ck_lan_tournament_escalation_ack_clock_us
        CHECK (clock_us >= 0),
    CONSTRAINT fk_lan_tournament_escalation_acks_episode_id
        FOREIGN KEY (episode_id)
        REFERENCES lan_tournament_match_due_episodes (id)
);

CREATE TABLE IF NOT EXISTS lan_tournament_match_dashboard_annotations (
    match_id UUID NOT NULL,
    tournament_id UUID NOT NULL,
    revision INTEGER NOT NULL DEFAULT '0',
    pinned_at TIMESTAMP NULL,
    pinned_by UUID NULL,
    updated_at TIMESTAMP NOT NULL,
    updated_by UUID NOT NULL,
    PRIMARY KEY (match_id),
    CONSTRAINT ck_lan_tournament_match_dashboard_annotations_revision
        CHECK (revision >= 0),
    CONSTRAINT ck_lan_tournament_match_dashboard_annotations_pin_pair
        CHECK ((pinned_at IS NULL AND pinned_by IS NULL)
            OR (pinned_at IS NOT NULL AND pinned_by IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS lan_tournament_dashboard_party_thresholds (
    party_id TEXT NOT NULL,
    yellow_minutes INTEGER NOT NULL,
    red_minutes INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT '1',
    updated_at TIMESTAMP NOT NULL,
    updated_by UUID NOT NULL,
    PRIMARY KEY (party_id),
    CONSTRAINT ck_lan_tournament_dashboard_party_thresholds_yellow_min
        CHECK (yellow_minutes >= 1),
    CONSTRAINT ck_lan_tournament_dashboard_party_thresholds_order
        CHECK (yellow_minutes < red_minutes),
    CONSTRAINT ck_lan_tournament_dashboard_party_thresholds_red_max
        CHECK (red_minutes <= 1440),
    CONSTRAINT ck_lan_tournament_dashboard_party_thresholds_revision
        CHECK (revision >= 1)
);

COMMIT;
