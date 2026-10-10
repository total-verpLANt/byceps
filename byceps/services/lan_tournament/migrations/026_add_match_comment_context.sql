-- Mark a match comment as written with a manual orga confirmation.
BEGIN;

ALTER TABLE lan_tournament_match_comments
    ADD COLUMN IF NOT EXISTS context TEXT NULL;

ALTER TABLE lan_tournament_match_comments
    DROP CONSTRAINT IF EXISTS ck_lan_tournament_match_comments_context;

ALTER TABLE lan_tournament_match_comments
    ADD CONSTRAINT ck_lan_tournament_match_comments_context
    CHECK (context IS NULL OR context IN ('orga_confirmation'));

COMMIT;
