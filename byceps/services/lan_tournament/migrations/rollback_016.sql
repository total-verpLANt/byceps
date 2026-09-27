-- Reverse migration 016: drop the created_from_request_id linkage,
-- the tournament requests indexes, the request audit log entries,
-- and the table.
--
-- Irreversible: this also deletes every lan_tournament_log_entries
-- row whose event_type starts with 'tournament-request-' (all
-- request audit history). Those entries carry no FK to the request
-- table, so they would otherwise survive its deletion as orphans.
-- Prefix match via starts_with(), not a LIKE wildcard: this file has
-- to stay free of a literal percent sign, since it also runs through
-- psycopg's client-side placeholder scanning in test harnesses.

BEGIN;

DROP INDEX IF EXISTS uq_lan_tournaments_created_from_request_id;

ALTER TABLE lan_tournaments
    DROP COLUMN IF EXISTS created_from_request_id;

DROP INDEX IF EXISTS ix_lan_tournament_requests_created_tournament_id;

DROP INDEX IF EXISTS ix_lan_tournament_requests_status;

DROP INDEX IF EXISTS ix_lan_tournament_requests_proposer_id;

DROP INDEX IF EXISTS ix_lan_tournament_requests_party_id;

DELETE FROM lan_tournament_log_entries
    WHERE starts_with(event_type, 'tournament-request-');

DROP TABLE IF EXISTS lan_tournament_requests;

COMMIT;
