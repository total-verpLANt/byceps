-- =================================================================
-- Migration 018: Add tournament images
-- =================================================================
-- Date: 2026-09-29
-- Description: Creates lan_tournament_images, the uploaded cover
--   images of the admin create wizard (PRD F-18), and adds
--   lan_tournaments.image_id, image_alt_text and creation_token.
--   Also grants lan_tournament.maintain (Wartung tab), a data-only
--   statement, see GRANT OF lan_tournament.maintain below.
--
-- Shape notes:
--   - UUID primary key (application-generated uuid7)
--   - FK to parties.id and FK to users.id for the uploader; the
--     party_id index is composite with created_at, so it backs both
--     the per-party picker and the purge-on-upload lookup of old
--     unreferenced images
--   - image_type TEXT holds the ImageType member name in lower case
--     and is limited to jpeg, png and webp by
--     ck_lan_tournament_images_image_type
--   - filename is display data only, 1 to 200 characters
--   - lan_tournaments.image_id is a nullable FK to
--     lan_tournament_images.id, indexed for the reference check on
--     image deletion
--   - lan_tournaments.image_alt_text is the per-tournament alt text
--     (empty or NULL means decorative)
--   - lan_tournaments.creation_token is the idempotency token of a
--     create, guarded by a partial UNIQUE index so a double submit
--     cannot produce a second tournament
--
-- The DO block below is the first idempotent ADD CONSTRAINT in this
-- module: PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS, so the
-- foreign key is guarded by a pg_constraint lookup on its name.
--
-- BYCEPS convention: NO CASCADE behaviors. Images are removed
-- explicitly at the application service layer.
--
-- =================================================================
-- GRANT OF lan_tournament.maintain
-- =================================================================
-- The Wartung tab of the admin (unused images, orphaned image files)
-- is guarded by `lan_tournament.maintain`. This section was appended
-- to 018 after the schema part had been applied on staging, so
-- staging re-runs the updated 018. Every schema statement above is an
-- idempotent no-op then (IF NOT EXISTS, pg_constraint guard); only the
-- grant inserts rows. No 019 exists for this.
--
-- WHY THIS IS A MIGRATION AND NOT `import-roles`
--
-- `flask import-roles` is create-only: in
-- byceps/services/authz/impex_service.py::_create_roles(), an
-- already-existing role takes the `continue` branch before its
-- assigned_permissions are applied. A re-run reports "skipped N roles"
-- and changes nothing, so existing roles never get the new
-- permission. Without this grant the Wartung routes answer 403 and the
-- tab is hidden. Migrations 015 and 017 fix the same problem for
-- their permissions.
--
-- WHICH ROLES
--
-- Data-driven, not a hardcoded role id: every role that already holds
-- `lan_tournament.administrate` gets `lan_tournament.maintain`.
-- Deployments do not necessarily use the `lan_tournament_admin` role
-- (staging holds a bespoke one). `lan_tournament_viewer` gets nothing:
-- it is read-only, and maintenance deletes data irreversibly.
--
-- NO SEPARATE PERMISSION TABLE
--
-- `authz_role_permissions.permission_id` carries no FK: BYCEPS has no
-- `authz_permissions` table, permissions are strings registered in
-- code at app start (byceps/services/lan_tournament/permissions.py).
-- The grant rows can be inserted directly.
--
-- Idempotent: IF NOT EXISTS on table, altered columns and all
-- indexes; pg_constraint guard on the foreign key; ON CONFLICT DO
-- NOTHING on the grant against the (role_id, permission_id) primary
-- key, so a re-run is a no-op and a grant made by hand is left alone.
-- Transaction-wrapped.
-- Rollback: rollback_018.sql
--
-- AFTER RUNNING: BYCEPS resolves a session's permissions on every
-- request, not at login. The grant takes effect on the next request;
-- no re-login needed.
-- =================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS lan_tournament_images (
    id          UUID        NOT NULL,
    party_id    TEXT        NOT NULL,
    creator_id  UUID        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    filename    TEXT        NOT NULL,
    image_type  TEXT        NOT NULL,
    width       INTEGER     NOT NULL,
    height      INTEGER     NOT NULL,
    byte_size   INTEGER     NOT NULL,
    CONSTRAINT pk_lan_tournament_images PRIMARY KEY (id),
    CONSTRAINT fk_lan_tournament_images_party_id
        FOREIGN KEY (party_id) REFERENCES parties (id),
    CONSTRAINT fk_lan_tournament_images_creator_id
        FOREIGN KEY (creator_id) REFERENCES users (id),
    CONSTRAINT ck_lan_tournament_images_filename_length
        CHECK (char_length(filename) BETWEEN 1 AND 200),
    CONSTRAINT ck_lan_tournament_images_image_type
        CHECK (image_type IN ('jpeg', 'png', 'webp')),
    CONSTRAINT ck_lan_tournament_images_dimensions
        CHECK (width > 0 AND height > 0),
    CONSTRAINT ck_lan_tournament_images_byte_size
        CHECK (byte_size > 0)
);

CREATE INDEX IF NOT EXISTS ix_lan_tournament_images_party_id_created_at
    ON lan_tournament_images (party_id, created_at);

ALTER TABLE lan_tournaments
    ADD COLUMN IF NOT EXISTS image_id UUID NULL,
    ADD COLUMN IF NOT EXISTS image_alt_text TEXT NULL,
    ADD COLUMN IF NOT EXISTS creation_token UUID NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_lan_tournaments_image_id'
    ) THEN
        ALTER TABLE lan_tournaments
            ADD CONSTRAINT fk_lan_tournaments_image_id
            FOREIGN KEY (image_id) REFERENCES lan_tournament_images (id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_lan_tournaments_image_id
    ON lan_tournaments (image_id);

-- Partial: most tournaments are created without a token (legacy rows,
-- request-based creates), and only rows with one need to be unique.
CREATE UNIQUE INDEX IF NOT EXISTS uq_lan_tournaments_creation_token
    ON lan_tournaments (creation_token)
    WHERE creation_token IS NOT NULL;

-- Every role that administrates LAN tournaments: maintain.
INSERT INTO authz_role_permissions (role_id, permission_id)
SELECT rp.role_id, 'lan_tournament.maintain'
FROM authz_role_permissions rp
WHERE rp.permission_id = 'lan_tournament.administrate'
ON CONFLICT DO NOTHING;

COMMIT;
