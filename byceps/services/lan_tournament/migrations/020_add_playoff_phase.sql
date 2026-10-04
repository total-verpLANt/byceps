-- =================================================================
-- Migration 020: Add the playoff phase
-- =================================================================
-- Date: 2026-09-30
-- Description: Adds the optional playoff phase of a LAN tournament
--   (PRD F-10): the playoff configuration and release state on
--   lan_tournaments, the phase marker on lan_tournament_matches and
--   the table lan_tournament_qualification_decisions, which holds an
--   orga's ordering of a tie.
--
-- Shape notes:
--   - lan_tournaments.playoff_game_format, playoff_elimination_mode,
--     playoff_group_count, playoff_qualifiers_per_group,
--     playoff_qualifier_count and playoff_release_mode are the
--     playoff configuration: all NULL (no playoffs) or one of the two
--     complete shapes enforced by ck_lan_tournaments_playoff_config
--     (round robin groups into a bracket, or highscore into FFA lobbies);
--     both shape branches are wrapped in COALESCE(..., FALSE), because
--     a CHECK passes on NULL and a half-set config would slip through
--   - playoff_auto_release_suspended, playoff_released_at and
--     playoff_released_by (FK to users.id) hold the release state;
--     leaderboard_closed_at backs the "close qualification" action
--   - lan_tournament_matches.phase is 1 (main phase) or 2 (playoffs),
--     ck_lan_tournament_matches_phase; existing rows become phase 1.
--     ix_lan_tournament_matches_tournament_phase backs the per-phase
--     match lookups
--   - lan_tournament_matches.seeding_target is the seeding draft that
--     generated the match ('initial', 'playoff' or 'ffa:<pool>:<round>');
--     NULL for matches made without a draft.
--     ix_lan_tournament_matches_tournament_seeding_target backs the
--     delete-by-target of a regeneration
--   - lan_tournament_qualification_decisions: UUID primary key, one
--     row per (tournament_id, scope) via
--     uq_lan_tournament_qualification_decisions_scope;
--     ordered_contestant_ids is a JSON array string; the reason must
--     not be empty after trimming spaces, tabs and line breaks
--   - timestamps are naive TIMESTAMP, as in the dbmodels
--
-- BYCEPS convention: NO CASCADE behaviors. Decisions are removed
-- explicitly at the application service layer.
--
-- Idempotent: IF NOT EXISTS on columns, indexes and the table;
-- PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS, so each added
-- constraint is guarded by a pg_constraint lookup on its name.
-- Transaction-wrapped.
-- Rollback: rollback_020.sql
-- =================================================================

BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE lan_tournaments
    ADD COLUMN IF NOT EXISTS playoff_game_format TEXT NULL,
    ADD COLUMN IF NOT EXISTS playoff_elimination_mode TEXT NULL,
    ADD COLUMN IF NOT EXISTS playoff_group_count INTEGER NULL,
    ADD COLUMN IF NOT EXISTS playoff_qualifiers_per_group INTEGER NULL,
    ADD COLUMN IF NOT EXISTS playoff_qualifier_count INTEGER NULL,
    ADD COLUMN IF NOT EXISTS playoff_release_mode TEXT NULL,
    ADD COLUMN IF NOT EXISTS playoff_auto_release_suspended BOOLEAN
        NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS playoff_released_at TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS playoff_released_by UUID NULL,
    ADD COLUMN IF NOT EXISTS leaderboard_closed_at TIMESTAMP NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_lan_tournaments_playoff_released_by'
    ) THEN
        ALTER TABLE lan_tournaments
            ADD CONSTRAINT fk_lan_tournaments_playoff_released_by
            FOREIGN KEY (playoff_released_by) REFERENCES users (id);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_lan_tournaments_playoff_config'
    ) THEN
        ALTER TABLE lan_tournaments
            ADD CONSTRAINT ck_lan_tournaments_playoff_config CHECK (
                (
                    playoff_game_format IS NULL
                    AND playoff_elimination_mode IS NULL
                    AND playoff_group_count IS NULL
                    AND playoff_qualifiers_per_group IS NULL
                    AND playoff_qualifier_count IS NULL
                    AND playoff_release_mode IS NULL
                ) OR COALESCE((
                    game_format = 'ONE_V_ONE'
                    AND elimination_mode = 'ROUND_ROBIN'
                    AND playoff_game_format = 'ONE_V_ONE'
                    AND playoff_elimination_mode IN
                        ('SINGLE_ELIMINATION', 'DOUBLE_ELIMINATION')
                    AND playoff_group_count >= 2
                    AND playoff_qualifiers_per_group >= 1
                    AND playoff_qualifier_count IS NULL
                    AND playoff_release_mode IS NOT NULL
                ), FALSE) OR COALESCE((
                    game_format = 'HIGHSCORE'
                    AND playoff_game_format = 'FREE_FOR_ALL'
                    AND playoff_elimination_mode IN
                        ('SINGLE_ELIMINATION', 'DOUBLE_ELIMINATION')
                    AND playoff_qualifier_count >= 2
                    AND playoff_group_count IS NULL
                    AND playoff_qualifiers_per_group IS NULL
                    AND playoff_release_mode IS NOT NULL
                ), FALSE)
            );
    END IF;
END $$;

ALTER TABLE lan_tournament_matches
    ADD COLUMN IF NOT EXISTS phase SMALLINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS seeding_target VARCHAR(40) NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_lan_tournament_matches_phase'
    ) THEN
        ALTER TABLE lan_tournament_matches
            ADD CONSTRAINT ck_lan_tournament_matches_phase
            CHECK (phase IN (1, 2));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_lan_tournament_matches_tournament_phase
    ON lan_tournament_matches (tournament_id, phase);

CREATE INDEX IF NOT EXISTS ix_lan_tournament_matches_tournament_seeding_target
    ON lan_tournament_matches (tournament_id, seeding_target);

CREATE TABLE IF NOT EXISTS lan_tournament_qualification_decisions (
    id                      UUID         NOT NULL,
    tournament_id           UUID         NOT NULL,
    scope                   VARCHAR(60)  NOT NULL,
    ordered_contestant_ids  TEXT         NOT NULL,
    reason                  TEXT         NOT NULL,
    decided_by              UUID         NOT NULL,
    decided_at              TIMESTAMP    NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_lan_tournament_qualification_decisions_tournament_id
        FOREIGN KEY (tournament_id) REFERENCES lan_tournaments (id),
    CONSTRAINT fk_lan_tournament_qualification_decisions_decided_by
        FOREIGN KEY (decided_by) REFERENCES users (id),
    CONSTRAINT uq_lan_tournament_qualification_decisions_scope
        UNIQUE (tournament_id, scope),
    CONSTRAINT ck_lan_tournament_qualification_decisions_reason
        CHECK (length(btrim(reason, E' \t\r\n')) > 0)
);

COMMIT;
