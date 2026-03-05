-- XOR constraint: exactly one contestant identity
ALTER TABLE lan_tournament_score_submissions
    ADD CONSTRAINT chk_score_contestant_xor
    CHECK (
        (participant_id IS NOT NULL AND team_id IS NULL)
        OR (participant_id IS NULL AND team_id IS NOT NULL)
    );

-- Score non-negativity
ALTER TABLE lan_tournament_score_submissions
    ADD CONSTRAINT chk_score_non_negative
    CHECK (score >= 0);

-- Composite index for leaderboard query
DROP INDEX IF EXISTS
    ix_lan_tournament_score_submissions_tournament_id;
CREATE INDEX ix_score_submissions_tournament_official
    ON lan_tournament_score_submissions
        (tournament_id, is_official);
