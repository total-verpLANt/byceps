-- Reverse migration 017: revoke the two tournament-request
-- permissions from every role that holds them.
--
-- WARNING -- read before running: this is NOT a precise undo, and it
-- cannot be. 017 inserts with ON CONFLICT DO NOTHING, so afterwards a
-- granted row is indistinguishable from one an operator had already
-- added by hand before 017 ran. This revokes ALL of them.
--
-- If any role was granted either permission deliberately and outside
-- 017, record it first and re-grant it after:
--
--   SELECT role_id, permission_id
--   FROM authz_role_permissions
--   WHERE permission_id IN
--       ('lan_tournament.request_view', 'lan_tournament.request_decide')
--   ORDER BY permission_id, role_id;
--
-- Consequence of running this: nobody can view or decide on
-- tournament requests through the admin surface. Participants can
-- still submit requests through the site surface; they simply pile
-- up unreviewed. The `lan_tournament.administrate` grant itself, and
-- every other permission grant, is untouched -- this only removes
-- the two rows this migration added.
--
-- Idempotent: deleting rows that are not there is a no-op.
-- Transaction-wrapped.
--
-- AFTER RUNNING: BYCEPS resolves a session's permissions on every
-- request, not at login, so the revoke takes effect on the very next
-- request -- not only once the user logs out and back in.

BEGIN;

DELETE FROM authz_role_permissions
WHERE permission_id IN (
    'lan_tournament.request_view',
    'lan_tournament.request_decide'
);

COMMIT;

-- Verify (expect zero rows):
--
--   SELECT role_id, permission_id
--   FROM authz_role_permissions
--   WHERE permission_id IN
--       ('lan_tournament.request_view', 'lan_tournament.request_decide');
