BEGIN;

ALTER TABLE lan_tournament_matches
    ADD COLUMN IF NOT EXISTS round INTEGER;

ALTER TABLE lan_tournament_matches
    ADD COLUMN IF NOT EXISTS next_match_id UUID
        REFERENCES lan_tournament_matches(id);

CREATE INDEX IF NOT EXISTS
    ix_lan_tournament_matches_next_match_id
    ON lan_tournament_matches(next_match_id);

COMMIT;
