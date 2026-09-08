-- Reverse migration 013: restore the FK from log entries to their
-- tournament.
--
-- WARNING -- read before running: ADD CONSTRAINT validates every
-- existing row. It WILL FAIL with a foreign key violation if any
-- lan_tournament_log_entries row has a tournament_id that no longer
-- exists in lan_tournaments. After 013 is applied, that is the
-- expected, intended outcome of every ordinary tournament deletion
-- (see tournament_service.py::delete_tournament() and the
-- 'tournament-deleted' entry it writes) -- not a data-integrity
-- accident to clean up.
--
-- This rollback deliberately does NOT delete or re-point orphaned
-- entries to force the ADD CONSTRAINT through. Those rows are the
-- audit trail migration 013 exists to protect; hard-deleting them
-- here to satisfy a validating FK would silently recreate the exact
-- defect 013 fixes (workspace-ytqz) -- the last thing a "rollback"
-- of that fix should do without an operator explicitly deciding to.
--
-- Before running this rollback in an environment where any
-- tournament has ever been deleted since 013 was applied, the
-- operator must first decide -- per whatever retention/compliance
-- policy applies -- what to do with orphaned entries, e.g.:
--   * accept keeping 013's FK-less schema permanently instead of
--     rolling back, or
--   * archive/export orphaned entries out of this table before
--     deleting them here, with that export treated as the system of
--     record going forward, or
--   * identify a placeholder/tombstone tournament row orphaned
--     entries can be legitimately re-pointed at.
-- There is no generic, non-destructive way to satisfy a validating
-- FK once entries have outlived their tournament, so no such
-- automatic step is included below.
--
-- If the table has no orphaned entries yet (e.g. rolling back
-- immediately after applying 013, before any tournament using it has
-- been deleted), this succeeds unchanged.

BEGIN;

ALTER TABLE lan_tournament_log_entries
    ADD CONSTRAINT fk_lan_tournament_log_entries_tournament_id
        FOREIGN KEY (tournament_id) REFERENCES lan_tournaments (id);

COMMIT;
