-- =================================================================
-- Migration 019: Add tournament seeding drafts
-- =================================================================
-- Date: 2026-09-30
-- Description: Creates lan_tournament_seedings, the server-held
--   seeding draft of a LAN tournament (PRD F-10). One row per
--   (tournament, target): the self-contained seed code the orga
--   edits, the code the bracket or lobbies were last generated from,
--   and a snapshot of the roster the code was built for.
--
-- Shape notes:
--   - UUID primary key (application-generated uuid7)
--   - FK to lan_tournaments.id and FK to users.id for the last editor
--   - target is 'initial', 'playoff' or 'ffa:<SE|WB|LB>:<round>',
--     enforced by ck_lan_tournament_seedings_target
--   - version is the optimistic-locking counter of the draft
--     (ck_lan_tournament_seedings_version: at least 1)
--   - roster_snapshot is a JSONB array of {"id", "label", "joined_late"}
--     objects in the codec's canonical order; "joined_late" is optional
--     (absent means false) and marks an entrant a re-seed appended until
--     the next generation; '[]' (legacy or empty) is valid
--   - created_at, updated_at and generated_at are naive TIMESTAMP,
--     as in the dbmodel
--   - no secondary index: the unique constraint on (tournament_id,
--     target) backs every lookup by tournament
--
-- BYCEPS convention: NO CASCADE behaviors. Seedings are removed
-- explicitly at the application service layer.
--
-- Idempotent: IF NOT EXISTS on the table; the table carries all
-- constraints inline, so an existing table is left untouched.
-- Transaction-wrapped.
-- Rollback: rollback_019.sql
-- =================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS lan_tournament_seedings (
    id                  UUID         NOT NULL,
    tournament_id       UUID         NOT NULL,
    target              VARCHAR(40)  NOT NULL,
    seed_code           TEXT         NOT NULL,
    version             INTEGER      NOT NULL DEFAULT 1,
    generated_seed_code TEXT         NULL,
    generated_at        TIMESTAMP    NULL,
    updated_by          UUID         NULL,
    roster_snapshot     JSONB        NOT NULL DEFAULT '[]'::jsonb,
    created_at          TIMESTAMP    NOT NULL,
    updated_at          TIMESTAMP    NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_lan_tournament_seedings_tournament_id
        FOREIGN KEY (tournament_id) REFERENCES lan_tournaments (id),
    CONSTRAINT fk_lan_tournament_seedings_updated_by
        FOREIGN KEY (updated_by) REFERENCES users (id),
    CONSTRAINT uq_lan_tournament_seedings_tournament_target
        UNIQUE (tournament_id, target),
    CONSTRAINT ck_lan_tournament_seedings_version
        CHECK (version >= 1),
    CONSTRAINT ck_lan_tournament_seedings_target
        CHECK (target IN ('initial', 'playoff')
            OR target ~ '^ffa:(SE|WB|LB):[0-9]{1,4}$')
);

COMMIT;
