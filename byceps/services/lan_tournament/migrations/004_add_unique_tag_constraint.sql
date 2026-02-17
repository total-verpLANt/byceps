-- Migration 004: Add case-insensitive partial unique indexes for team name and tag
--
-- Replaces the full UNIQUE(tournament_id, name) constraint with partial
-- unique indexes scoped to active (non-soft-deleted) teams.
-- Names use LOWER(), tags use UPPER() for case-insensitive uniqueness.

-- Drop old full unique constraint
ALTER TABLE lan_tournament_teams
    DROP CONSTRAINT IF EXISTS uq_lan_tournament_teams_tournament_id_name;

-- Case-insensitive partial unique index on name for active teams
CREATE UNIQUE INDEX uq_lan_tournament_teams_active_name_ci
    ON lan_tournament_teams (tournament_id, LOWER(name))
    WHERE removed_at IS NULL;

-- Case-insensitive partial unique index on tag for active teams with a tag
CREATE UNIQUE INDEX uq_lan_tournament_teams_active_tag_ci
    ON lan_tournament_teams (tournament_id, UPPER(tag))
    WHERE removed_at IS NULL AND tag IS NOT NULL;
