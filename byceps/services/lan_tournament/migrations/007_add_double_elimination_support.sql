-- Add double elimination support columns to matches table
ALTER TABLE lan_tournament_matches
    ADD COLUMN bracket VARCHAR(8) NULL,
    ADD COLUMN loser_next_match_id UUID NULL
        REFERENCES lan_tournament_matches (id);

CREATE INDEX ix_lan_tournament_matches_bracket
    ON lan_tournament_matches (bracket);
CREATE INDEX ix_lan_tournament_matches_loser_next_match_id
    ON lan_tournament_matches (loser_next_match_id);
