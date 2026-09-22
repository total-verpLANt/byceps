-- Reverse migration 014: drop orga assignment indexes and table.

BEGIN;

DROP INDEX IF EXISTS ix_lan_tournament_orgas_user_id;

DROP INDEX IF EXISTS ix_lan_tournament_orgas_tournament_id;

DROP TABLE IF EXISTS lan_tournament_orgas;

COMMIT;
