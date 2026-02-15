-- Rollback: drop soft-delete columns and indexes, revert constraint

DROP INDEX IF EXISTS ix_lan_tournament_participants_active;
DROP INDEX IF EXISTS ix_lan_tournament_teams_active;

ALTER TABLE lan_tournament_match_contestants
    DROP CONSTRAINT IF EXISTS ck_exactly_one_contestant;

ALTER TABLE lan_tournament_match_contestants
    ADD CONSTRAINT ck_exactly_one_contestant CHECK (
        (team_id IS NOT NULL AND participant_id IS NULL)
        OR (team_id IS NULL AND participant_id IS NOT NULL)
    );

ALTER TABLE lan_tournament_participants
    DROP COLUMN IF EXISTS removed_at;
ALTER TABLE lan_tournament_teams
    DROP COLUMN IF EXISTS removed_at;
