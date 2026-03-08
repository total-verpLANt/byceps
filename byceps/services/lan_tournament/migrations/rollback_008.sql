ALTER TABLE lan_tournaments
    DROP COLUMN IF EXISTS winner_participant_id,
    DROP COLUMN IF EXISTS winner_team_id;
