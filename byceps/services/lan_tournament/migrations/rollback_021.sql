-- Rollback for 021_add_match_ready_columns.sql.
-- Drops the F-04 readiness columns. Transaction-wrapped, idempotent.

BEGIN;

ALTER TABLE lan_tournament_matches
    DROP COLUMN IF EXISTS both_ready_notified_at;

ALTER TABLE lan_tournament_matches
    DROP COLUMN IF EXISTS ready_by_b;
ALTER TABLE lan_tournament_matches
    DROP COLUMN IF EXISTS ready_by_a;
ALTER TABLE lan_tournament_matches
    DROP COLUMN IF EXISTS ready_at_b;
ALTER TABLE lan_tournament_matches
    DROP COLUMN IF EXISTS ready_at_a;

ALTER TABLE lan_tournament_matches
    DROP COLUMN IF EXISTS occupied_since;

COMMIT;
