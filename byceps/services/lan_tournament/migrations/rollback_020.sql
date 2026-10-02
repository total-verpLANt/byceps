-- Reverse migration 020: drop the qualification decisions table, the
-- match phase and seeding target markers and the playoff columns on
-- lan_tournaments.
--
-- Irreversible: every lan_tournament_qualification_decisions row is
-- lost, every tournament loses its playoff configuration and release
-- state, and every match loses its phase marker. Once playoffs ran,
-- the phase-2 matches stay in lan_tournament_matches but are no longer
-- distinguishable from phase-1 matches, so the bracket mixes both
-- phases. Do not run this after playoffs were generated.

BEGIN;

DROP TABLE IF EXISTS lan_tournament_qualification_decisions;

DROP INDEX IF EXISTS ix_lan_tournament_matches_tournament_seeding_target;

DROP INDEX IF EXISTS ix_lan_tournament_matches_tournament_phase;

ALTER TABLE lan_tournament_matches
    DROP CONSTRAINT IF EXISTS ck_lan_tournament_matches_phase;

ALTER TABLE lan_tournament_matches
    DROP COLUMN IF EXISTS seeding_target,
    DROP COLUMN IF EXISTS phase;

ALTER TABLE lan_tournaments
    DROP CONSTRAINT IF EXISTS ck_lan_tournaments_playoff_config;

ALTER TABLE lan_tournaments
    DROP CONSTRAINT IF EXISTS fk_lan_tournaments_playoff_released_by;

ALTER TABLE lan_tournaments
    DROP COLUMN IF EXISTS leaderboard_closed_at,
    DROP COLUMN IF EXISTS playoff_released_by,
    DROP COLUMN IF EXISTS playoff_released_at,
    DROP COLUMN IF EXISTS playoff_auto_release_suspended,
    DROP COLUMN IF EXISTS playoff_release_mode,
    DROP COLUMN IF EXISTS playoff_qualifier_count,
    DROP COLUMN IF EXISTS playoff_qualifiers_per_group,
    DROP COLUMN IF EXISTS playoff_group_count,
    DROP COLUMN IF EXISTS playoff_elimination_mode,
    DROP COLUMN IF EXISTS playoff_game_format;

COMMIT;
