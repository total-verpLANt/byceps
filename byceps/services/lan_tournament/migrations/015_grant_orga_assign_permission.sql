-- =================================================================
-- Migration 015: Grant the lan_tournament.orga_assign permission
-- =================================================================
-- Date: 2026-09-26
-- Description: Assigns the permission introduced alongside migration
--   014 to the roles that administrate LAN tournaments. Data-only --
--   no schema change.
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
-- re-run reports "Imported 0 roles, skipped 2 roles" and changes
-- nothing. Observed on staging: the Orgas tab rendered its (empty)
-- list but no assign form, and POST .../orgas/assign answered 403 --
-- the feature looked merely unused rather than ungranted. The BYCEPS
-- authz admin blueprint is read-only (GET routes only), so there is
-- no UI path either. That leaves SQL.
--
-- WHICH ROLES
--
-- Data-driven, not a hardcoded role id: every role that already holds
-- `lan_tournament.administrate` gets `lan_tournament.orga_assign`.
-- Deployments do not necessarily use the `lan_tournament_admin` role
-- from lan_tournament_roles.toml -- staging holds a bespoke one -- so
-- naming a single role here would silently fix nothing on exactly the
-- installations that need it most.
--
-- The rule is sound in the other direction too: `administrate` is
-- already the strongest permission in the module (confirm, retract and
-- correct results, cancel a tournament). Someone trusted with those
-- being able to appoint an orga is not an escalation of consequence.
-- Statement 2 additionally covers the canonical role by name, in case
-- an install carries it without `administrate`.
--
-- Idempotent: ON CONFLICT DO NOTHING against the
-- (role_id, permission_id) primary key, so a re-run is a no-op and
-- an install that was already granted by hand is left alone.
-- Transaction-wrapped.
-- Rollback: rollback_015.sql
--
-- AFTER RUNNING: BYCEPS resolves a session's permissions at login.
-- Anyone already signed in must log out and back in before the grant
-- takes effect.
-- =================================================================

BEGIN;

-- 1. Every role that administrates LAN tournaments.
INSERT INTO authz_role_permissions (role_id, permission_id)
SELECT rp.role_id, 'lan_tournament.orga_assign'
FROM authz_role_permissions rp
WHERE rp.permission_id = 'lan_tournament.administrate'
ON CONFLICT DO NOTHING;

-- 2. The canonical role from lan_tournament_roles.toml, by name, in
--    case it exists without `lan_tournament.administrate`.
INSERT INTO authz_role_permissions (role_id, permission_id)
SELECT r.id, 'lan_tournament.orga_assign'
FROM authz_roles r
WHERE r.id = 'lan_tournament_admin'
ON CONFLICT DO NOTHING;

COMMIT;

-- Verify (expect one row per administrating role):
--
--   SELECT role_id
--   FROM authz_role_permissions
--   WHERE permission_id = 'lan_tournament.orga_assign'
--   ORDER BY role_id;
