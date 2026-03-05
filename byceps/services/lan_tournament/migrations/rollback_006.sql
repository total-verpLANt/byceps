ALTER TABLE lan_tournament_score_submissions
    DROP CONSTRAINT IF EXISTS chk_score_contestant_xor;
ALTER TABLE lan_tournament_score_submissions
    DROP CONSTRAINT IF EXISTS chk_score_non_negative;
DROP INDEX IF EXISTS ix_score_submissions_tournament_official;
CREATE INDEX ix_lan_tournament_score_submissions_tournament_id
    ON lan_tournament_score_submissions (tournament_id);
