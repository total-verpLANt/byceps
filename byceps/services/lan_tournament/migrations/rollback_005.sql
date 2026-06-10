DROP TABLE IF EXISTS lan_tournament_score_submissions;
ALTER TABLE lan_tournaments
    DROP COLUMN IF EXISTS score_ordering;
