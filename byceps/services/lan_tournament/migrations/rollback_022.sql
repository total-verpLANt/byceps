-- HUMAN APPROVAL REQUIRED. Irreversible loss of all new pairing/work history,
-- generation/revision and hold facts. Stop repaired web/workers before rollback.
-- Preserves all 021 readiness/occupancy/revocation columns and base structures.
BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE lan_tournament_matches
    DROP CONSTRAINT IF EXISTS fk_lan_tournament_matches_pairing_id,
    DROP CONSTRAINT IF EXISTS ck_lan_tournament_matches_pairing_generation,
    DROP CONSTRAINT IF EXISTS ck_lan_tournament_matches_readiness_revision,
    DROP COLUMN IF EXISTS pairing_id,
    DROP COLUMN IF EXISTS pairing_generation,
    DROP COLUMN IF EXISTS readiness_revision,
    DROP COLUMN IF EXISTS invitation_hold_a,
    DROP COLUMN IF EXISTS invitation_hold_b;
DROP TABLE IF EXISTS lan_tournament_match_invitations;
DROP TABLE IF EXISTS lan_tournament_match_pairings;

COMMIT;
