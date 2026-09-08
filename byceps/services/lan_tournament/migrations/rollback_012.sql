-- Reverse migration 012: drop log entries index and table.

BEGIN;

DROP INDEX IF EXISTS ix_lan_tournament_log_entries_tournament_id;

DROP TABLE IF EXISTS lan_tournament_log_entries;

COMMIT;
