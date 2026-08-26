-- 021: F-04 match-ready system — per-side readiness claims.
--
-- Adds per-side ready timestamps + claimers, occupancy start and the
-- both-ready notification marker (email suppression) to
-- lan_tournament_matches.
--
-- Idempotent, transaction-wrapped, zero CASCADE behaviors.

BEGIN;

ALTER TABLE lan_tournament_matches
    ADD COLUMN IF NOT EXISTS occupied_since TIMESTAMP;

ALTER TABLE lan_tournament_matches
    ADD COLUMN IF NOT EXISTS ready_at_a TIMESTAMP;
ALTER TABLE lan_tournament_matches
    ADD COLUMN IF NOT EXISTS ready_at_b TIMESTAMP;

ALTER TABLE lan_tournament_matches
    ADD COLUMN IF NOT EXISTS ready_by_a UUID REFERENCES users (id);
ALTER TABLE lan_tournament_matches
    ADD COLUMN IF NOT EXISTS ready_by_b UUID REFERENCES users (id);

ALTER TABLE lan_tournament_matches
    ADD COLUMN IF NOT EXISTS both_ready_notified_at TIMESTAMP;

-- Backfill: a match is occupied once both sides are fixed. The
-- occupancy start is derived from the later of the two contestant
-- creation timestamps. Existing fully-occupied matches only.
UPDATE lan_tournament_matches m
SET occupied_since = occ.ts
FROM (
    SELECT mc.tournament_match_id AS match_id,
           max(mc.created_at) AS ts
    FROM lan_tournament_match_contestants mc
    WHERE mc.participant_id IS NOT NULL
       OR mc.team_id IS NOT NULL
    GROUP BY mc.tournament_match_id
    HAVING count(*) = 2
) occ
WHERE m.id = occ.match_id
  AND m.occupied_since IS NULL;

COMMIT;
