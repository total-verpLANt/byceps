-- HUMAN APPROVAL REQUIRED. Irreversible loss of every due episode,
-- escalation acknowledgement, dashboard pin and party threshold override,
-- and of the operational clock and last-change facts. Stop the web and
-- worker processes before the rollback and install code that predates 023.
-- Touches only the objects 023 added; no CASCADE. Every other column,
-- constraint and index of lan_tournaments and lan_tournament_matches survives.
BEGIN;
SET LOCAL lock_timeout = '5s';

-- Tournament first, then matches, as in 023.
ALTER TABLE lan_tournaments
    DROP CONSTRAINT IF EXISTS ck_lan_tournaments_operational_clock_elapsed_us,
    DROP COLUMN IF EXISTS operational_clock_activated_at,
    DROP COLUMN IF EXISTS operational_clock_running_since,
    DROP COLUMN IF EXISTS operational_clock_elapsed_us;

ALTER TABLE lan_tournament_matches
    DROP COLUMN IF EXISTS last_changed_at;

-- The acknowledgement foreign key points at the episode table: drop it first.
DROP TABLE IF EXISTS lan_tournament_match_escalation_acks;
DROP TABLE IF EXISTS lan_tournament_match_due_episodes;
DROP TABLE IF EXISTS lan_tournament_match_dashboard_annotations;
DROP TABLE IF EXISTS lan_tournament_dashboard_party_thresholds;

COMMIT;
