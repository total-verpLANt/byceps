-- Category selections are lost; all other tournament data is preserved.
BEGIN;
ALTER TABLE lan_tournaments DROP CONSTRAINT IF EXISTS ck_lan_tournaments_category;
ALTER TABLE lan_tournaments DROP COLUMN IF EXISTS category;
COMMIT;
