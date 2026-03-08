DROP INDEX IF EXISTS ix_lan_tournament_matches_loser_next_match_id;
DROP INDEX IF EXISTS ix_lan_tournament_matches_bracket;
ALTER TABLE lan_tournament_matches
    DROP COLUMN IF EXISTS loser_next_match_id,
    DROP COLUMN IF EXISTS bracket;
