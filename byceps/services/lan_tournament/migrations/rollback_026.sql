-- Remove the match comment context marker; the comments themselves are kept.
BEGIN;

ALTER TABLE lan_tournament_match_comments
    DROP CONSTRAINT IF EXISTS ck_lan_tournament_match_comments_context;

ALTER TABLE lan_tournament_match_comments
    DROP COLUMN IF EXISTS context;

COMMIT;
