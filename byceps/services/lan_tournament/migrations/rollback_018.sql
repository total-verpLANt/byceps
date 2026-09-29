-- Reverse migration 018: revoke lan_tournament.maintain, drop the
-- creation token, image columns and image reference on
-- lan_tournaments, then the images table.
--
-- WARNING -- the revoke is NOT a precise undo. 018 grants with ON
-- CONFLICT DO NOTHING, so afterwards a granted row is indistinguishable
-- from one an operator added by hand. This revokes ALL
-- `lan_tournament.maintain` grants. To keep a deliberate one, record
-- it first and re-grant it afterwards:
--
--   SELECT role_id FROM authz_role_permissions
--   WHERE permission_id = 'lan_tournament.maintain';
--
-- Do not run this on a database that is already on 018 just to
-- re-apply the grant: it drops the image table and the image columns.
-- Re-run the updated 018 instead.
--
-- Irreversible: every lan_tournament_images row is lost, and every
-- tournament loses its image_id, image_alt_text and creation_token.
-- The image files under data/ are not touched. Tournaments that used
-- an uploaded image keep their relative image_url.

BEGIN;

DELETE FROM authz_role_permissions
WHERE permission_id = 'lan_tournament.maintain';

DROP INDEX IF EXISTS uq_lan_tournaments_creation_token;

DROP INDEX IF EXISTS ix_lan_tournaments_image_id;

ALTER TABLE lan_tournaments
    DROP CONSTRAINT IF EXISTS fk_lan_tournaments_image_id;

ALTER TABLE lan_tournaments
    DROP COLUMN IF EXISTS creation_token,
    DROP COLUMN IF EXISTS image_alt_text,
    DROP COLUMN IF EXISTS image_id;

DROP INDEX IF EXISTS ix_lan_tournament_images_party_id_created_at;

DROP TABLE IF EXISTS lan_tournament_images;

COMMIT;
