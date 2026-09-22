-- Reverse migration 015: revoke the lan_tournament.orga_assign
-- permission from every role that holds it.
--
-- WARNING -- read before running: this is NOT a precise undo, and it
-- cannot be. 015 inserts with ON CONFLICT DO NOTHING, so afterwards a
-- granted row is indistinguishable from one an operator had already
-- added by hand before 015 ran. This revokes ALL of them.
--
-- If any role was granted `lan_tournament.orga_assign` deliberately
-- and outside 015, record it first and re-grant it after:
--
--   SELECT role_id
--   FROM authz_role_permissions
--   WHERE permission_id = 'lan_tournament.orga_assign'
--   ORDER BY role_id;
--
-- Consequence of running this: no one can appoint or revoke a
-- tournament orga. Existing orga assignments in
-- lan_tournament_orgas are NOT touched -- the people already
-- appointed keep their scoped site-side rights, since those are
-- checked against that table (see blueprints/site/authz.py), not
-- against this permission. Only the admin-side appointment surface
-- goes away. Use rollback_014.sql to remove the assignments
-- themselves.
--
-- Idempotent: deleting rows that are not there is a no-op.
-- Transaction-wrapped.
--
-- AFTER RUNNING: BYCEPS resolves a session's permissions at login, so
-- anyone already signed in keeps the permission until they log out
-- and back in.

BEGIN;

DELETE FROM authz_role_permissions
WHERE permission_id = 'lan_tournament.orga_assign';

COMMIT;

-- Verify (expect zero rows):
--
--   SELECT role_id
--   FROM authz_role_permissions
--   WHERE permission_id = 'lan_tournament.orga_assign';
