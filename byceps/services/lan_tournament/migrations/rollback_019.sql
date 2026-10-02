-- Reverse migration 019: drop the tournament seeding drafts table.
--
-- Irreversible: every lan_tournament_seedings row is lost (seed codes,
-- generated codes and roster snapshots). Brackets and lobbies already
-- generated from a draft are not touched.

BEGIN;

DROP TABLE IF EXISTS lan_tournament_seedings;

COMMIT;
