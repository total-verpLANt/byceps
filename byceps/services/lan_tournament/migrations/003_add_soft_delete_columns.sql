-- Add soft-delete columns to participants and teams
ALTER TABLE lan_tournament_participants
    ADD COLUMN removed_at TIMESTAMPTZ DEFAULT NULL;
ALTER TABLE lan_tournament_teams
    ADD COLUMN removed_at TIMESTAMPTZ DEFAULT NULL;

-- Clean up any orphaned rows from old approach
DELETE FROM lan_tournament_match_contestants
    WHERE team_id IS NULL AND participant_id IS NULL;

-- Revert to strict constraint
ALTER TABLE lan_tournament_match_contestants
    DROP CONSTRAINT IF EXISTS ck_at_most_one_contestant;
ALTER TABLE lan_tournament_match_contestants
    ADD CONSTRAINT ck_exactly_one_contestant CHECK (
        (team_id IS NOT NULL AND participant_id IS NULL)
        OR (team_id IS NULL AND participant_id IS NOT NULL)
    );

CREATE INDEX ix_lan_tournament_participants_active
    ON lan_tournament_participants (tournament_id) WHERE removed_at IS NULL;
CREATE INDEX ix_lan_tournament_teams_active
    ON lan_tournament_teams (tournament_id) WHERE removed_at IS NULL;
