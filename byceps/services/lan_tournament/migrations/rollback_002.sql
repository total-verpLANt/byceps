BEGIN;

DROP INDEX IF EXISTS ix_lan_tournament_matches_next_match_id;

ALTER TABLE lan_tournament_matches
    DROP COLUMN IF EXISTS next_match_id;

ALTER TABLE lan_tournament_matches
    DROP COLUMN IF EXISTS round;

COMMIT;
