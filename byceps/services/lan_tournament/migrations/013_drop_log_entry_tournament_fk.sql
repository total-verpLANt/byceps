-- =================================================================
-- Migration 013: Drop tournament FK from log entries
-- =================================================================
-- Date: 2026-09-13
-- Description: Drops fk_lan_tournament_log_entries_tournament_id.
--   The column, its NOT NULL constraint and its index
--   (ix_lan_tournament_log_entries_tournament_id) are all kept --
--   tournament_id remains a meaningful identifier and the
--   per-tournament query still uses it. Only the FK goes.
--
-- Why: this FK forced every lan_tournament_log_entries row for a
--   tournament to be deleted before the tournament row itself could
--   go (DELETE FROM lan_tournaments would otherwise violate it).
--   tournament_service.py::delete_tournament() used that to justify
--   purging the audit log as step 1 of its own cascade, which let
--   the same role the log exists to police (lan_tournament
--   administrators) also erase it by deleting the tournament.
--   workspace-ytqz.
--
--   As of this migration, delete_tournament() no longer deletes log
--   entries at all; deleting a tournament orphans its entries
--   (tournament_id now points at a row that no longer exists) and
--   writes one further 'tournament-deleted' entry carrying enough
--   denormalised tournament context (name, party, game, status) to
--   stay meaningful with nothing left to join to. See
--   tournament_service.py and tournament_log_service.py.
--
--   The only thing that still removes log entries is the retention
--   purge (`byceps purge-lan-tournament-log-entries`), which filters
--   on occurred_at alone and has no dependency on the FK -- orphaned
--   entries age out exactly like any other entry.
--
-- BYCEPS convention: NO CASCADE. This migration does not add one;
--   it removes the one constraint that stood in for the missing
--   CASCADE and forced deletion at the wrong layer.
--
-- Idempotent: DROP CONSTRAINT IF EXISTS.
-- Transaction-wrapped.
-- Rollback: rollback_013.sql (see that file for why re-adding this
--   FK is not, in general, a safe/automatic operation once entries
--   have outlived their tournament).
-- =================================================================

BEGIN;

ALTER TABLE lan_tournament_log_entries
    DROP CONSTRAINT IF EXISTS fk_lan_tournament_log_entries_tournament_id;

COMMIT;
