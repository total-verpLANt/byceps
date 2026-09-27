-- =================================================================
-- Migration 017: Grant the tournament request permissions
-- =================================================================
-- Date: 2026-09-27
-- Description: Assigns the two permissions introduced alongside the
--   tournament-request feature (migration 016) to the roles that
--   administrate LAN tournaments. Data-only -- no schema change.
--
-- WHY THIS IS A MIGRATION AND NOT `import-roles`
--
-- `flask import-roles -f scripts/data/lan_tournament_roles.toml` does
-- NOT do this. It is create-only. In
-- byceps/services/authz/impex_service.py::_create_roles(), an
-- already-existing role takes the `continue` branch BEFORE its
-- assigned_permissions are applied:
--
--     role_result = authz_service.create_role(role_id, role_title)
--     if role_result.is_ok():
--         imported_roles_count += 1
--     else:
--         # Role exists; skip.
--         skipped_roles_count += 1
--         continue          # <-- assigned_permissions never applied
--
-- So on every deployment that ran the import before this branch, a
-- re-run reports "Imported 0 roles, skipped N roles" and changes
-- nothing. Without this grant, every request-related admin route
-- (queue, detail, accept, reject, edit) answers 403 and the nav tab
-- is hidden, while participants can keep submitting requests -- the
-- feature looks merely unused rather than ungranted. This is exactly
-- the pattern migration 015 fixed for `lan_tournament.orga_assign`;
-- see that file's header for the fuller writeup.
--
-- WHICH ROLES
--
-- Data-driven, not a hardcoded role id: every role that already holds
-- `lan_tournament.administrate` gets both permissions. Deployments do
-- not necessarily use the `lan_tournament_admin` role from
-- lan_tournament_roles.toml -- staging holds a bespoke one -- so
-- naming a single role here would silently fix nothing on exactly the
-- installations that need it most.
--
-- The rule is sound in the other direction too: `administrate` is
-- already the strongest permission in the module (confirm, retract
-- and correct results, cancel a tournament). Someone trusted with
-- that can also be trusted to decide on a tournament request.
--
-- `lan_tournament_viewer` gets neither permission: it is a read-only
-- role and request review is a privileged action.
--
-- NO SEPARATE PERMISSION TABLE
--
-- Unlike a table with a foreign key, `authz_role_permissions.
-- permission_id` (see byceps/services/authz/dbmodels.py) carries no
-- FK -- BYCEPS has no `authz_permissions` table. Permissions exist
-- only as strings registered in code at app start
-- (byceps/util/authz.py::register_permissions, called from
-- byceps/services/lan_tournament/permissions.py). This migration can
-- therefore insert the grant rows directly; there is no companion
-- table to populate first, and 015 relies on the same fact.
--
-- Idempotent: ON CONFLICT DO NOTHING against the
-- (role_id, permission_id) primary key, so a re-run is a no-op and
-- an install that was already granted by hand is left alone.
-- Transaction-wrapped.
-- Rollback: rollback_017.sql
--
-- AFTER RUNNING: BYCEPS resolves a session's permissions on every
-- request (byceps/util/user_session.py::get_current_user, called from
-- each blueprint's before_app_request), not at login. The grant takes
-- effect on the next request; no re-login needed.
-- =================================================================

BEGIN;

-- 1. Every role that administrates LAN tournaments: request_view.
INSERT INTO authz_role_permissions (role_id, permission_id)
SELECT rp.role_id, 'lan_tournament.request_view'
FROM authz_role_permissions rp
WHERE rp.permission_id = 'lan_tournament.administrate'
ON CONFLICT DO NOTHING;

-- 2. Every role that administrates LAN tournaments: request_decide.
INSERT INTO authz_role_permissions (role_id, permission_id)
SELECT rp.role_id, 'lan_tournament.request_decide'
FROM authz_role_permissions rp
WHERE rp.permission_id = 'lan_tournament.administrate'
ON CONFLICT DO NOTHING;

COMMIT;

-- Verify (expect one row per administrating role, per permission):
--
--   SELECT role_id, permission_id
--   FROM authz_role_permissions
--   WHERE permission_id IN
--       ('lan_tournament.request_view', 'lan_tournament.request_decide')
--   ORDER BY permission_id, role_id;
