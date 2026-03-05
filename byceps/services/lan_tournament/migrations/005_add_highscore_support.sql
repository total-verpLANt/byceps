-- Add score ordering to tournaments
ALTER TABLE lan_tournaments
    ADD COLUMN score_ordering TEXT NULL;

-- Create score submissions table for highscore mode
CREATE TABLE lan_tournament_score_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tournament_id UUID NOT NULL
        REFERENCES lan_tournaments (id),
    participant_id UUID NULL
        REFERENCES lan_tournament_participants (id),
    team_id UUID NULL
        REFERENCES lan_tournament_teams (id),
    score BIGINT NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_by UUID NULL
        REFERENCES users (id),
    is_official BOOLEAN NOT NULL DEFAULT TRUE,
    note TEXT NULL
);

CREATE INDEX ix_lan_tournament_score_submissions_tournament_id
    ON lan_tournament_score_submissions (tournament_id);
