-- Rollback migration 004: Restore original unique constraint

-- Drop case-insensitive partial unique indexes
DROP INDEX IF EXISTS uq_lan_tournament_teams_active_name_ci;
DROP INDEX IF EXISTS uq_lan_tournament_teams_active_tag_ci;

-- Restore original full unique constraint
ALTER TABLE lan_tournament_teams
    ADD CONSTRAINT uq_lan_tournament_teams_tournament_id_name
    UNIQUE (tournament_id, name);
