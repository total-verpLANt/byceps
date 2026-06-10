-- =================================================================
-- LAN Tournament Module - Complete Schema Migration
-- =================================================================
-- Migration: 001_create_lan_tournament_schema
-- Date: 2026-02-08
-- Description: Creates all tables for BYCEPS LAN tournament system
--
-- This is the consolidated initial migration combining:
--   - Original 001: Core table structure
--   - Original 002: Team name unique constraint
--   - Original 003: Updated_at timestamp columns
--
-- Tables Created (in dependency order):
--   1. lan_tournaments (main tournament registry)
--   2. lan_tournament_teams (team definitions)
--   3. lan_tournament_participants (player registrations)
--   4. lan_tournament_matches (match records)
--   5. lan_tournament_match_contestants (match participants)
--   6. lan_tournament_match_comments (match discussion)
--
-- =================================================================
-- BYCEPS Foreign Key Policy: NO CASCADE/SET NULL
-- =================================================================
-- CRITICAL: This migration follows BYCEPS convention of NO database-level
-- cascade behaviors. All foreign keys use the default ON DELETE NO ACTION.
--
-- WHY NO CASCADE:
-- - Application maintains full control of deletion order
-- - Domain events are emitted for each cleanup operation
-- - Audit trail of all deletions is preserved
-- - Explicit business logic for cascading operations
-- - Prevention of accidental data loss from DB-level cascades
--
-- CASCADE HANDLING: Performed at service layer in:
--   - tournament_service.py::delete_tournament()
--   - tournament_team_service.py::delete_team()
--   - tournament_match_service.py::delete_match()
--
-- See /workspace/tests/unit/services/lan_tournament/test_tournament_deletion.py
-- for comprehensive tests of CASCADE behavior.
-- =================================================================

BEGIN;

-- =================================================================
-- Table 1: lan_tournaments (root entity)
-- =================================================================
CREATE TABLE IF NOT EXISTS lan_tournaments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    party_id TEXT NOT NULL REFERENCES parties(id),
    name TEXT NOT NULL,
    game TEXT,
    description TEXT,
    image_url TEXT,
    ruleset TEXT,
    start_time TIMESTAMP,
    min_players INTEGER,
    max_players INTEGER,
    min_teams INTEGER,
    max_teams INTEGER,
    min_players_in_team INTEGER,
    max_players_in_team INTEGER,
    contestant_type TEXT,
    tournament_status TEXT,
    tournament_mode TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP,
    CONSTRAINT ck_min_players_positive CHECK (min_players IS NULL OR min_players > 0),
    CONSTRAINT ck_max_players_positive CHECK (max_players IS NULL OR max_players > 0),
    CONSTRAINT ck_max_players_gte_min CHECK (
        min_players IS NULL OR max_players IS NULL OR max_players >= min_players
    ),
    CONSTRAINT ck_min_teams_positive CHECK (min_teams IS NULL OR min_teams > 0),
    CONSTRAINT ck_max_teams_positive CHECK (max_teams IS NULL OR max_teams > 0),
    CONSTRAINT ck_max_teams_gte_min CHECK (
        min_teams IS NULL OR max_teams IS NULL OR max_teams >= min_teams
    ),
    CONSTRAINT ck_min_players_in_team_positive CHECK (
        min_players_in_team IS NULL OR min_players_in_team >= 1
    ),
    CONSTRAINT ck_max_players_in_team_positive CHECK (
        max_players_in_team IS NULL OR max_players_in_team >= 1
    ),
    CONSTRAINT ck_max_players_in_team_gte_min CHECK (
        min_players_in_team IS NULL OR max_players_in_team IS NULL OR
        max_players_in_team >= min_players_in_team
    )
);

CREATE INDEX IF NOT EXISTS ix_lan_tournaments_party_id
    ON lan_tournaments(party_id);


-- =================================================================
-- Table 2: lan_tournament_teams (team definitions)
-- =================================================================
CREATE TABLE IF NOT EXISTS lan_tournament_teams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tournament_id UUID NOT NULL REFERENCES lan_tournaments(id),
    name TEXT NOT NULL,
    tag TEXT,
    description TEXT,
    image_url TEXT,
    captain_user_id UUID NOT NULL REFERENCES users(id),
    join_code TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP,
    CONSTRAINT uq_lan_tournament_teams_tournament_id_name
        UNIQUE (tournament_id, name)
);

CREATE INDEX IF NOT EXISTS ix_lan_tournament_teams_tournament_id
    ON lan_tournament_teams(tournament_id);


-- =================================================================
-- Table 3: lan_tournament_participants (player registrations)
-- =================================================================
CREATE TABLE IF NOT EXISTS lan_tournament_participants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    tournament_id UUID NOT NULL REFERENCES lan_tournaments(id),
    substitute_player BOOLEAN NOT NULL DEFAULT FALSE,
    team_id UUID REFERENCES lan_tournament_teams(id),
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT uq_lan_tournament_participants_tournament_id_user_id
        UNIQUE (tournament_id, user_id)
);

CREATE INDEX IF NOT EXISTS ix_lan_tournament_participants_tournament_id
    ON lan_tournament_participants(tournament_id);

CREATE INDEX IF NOT EXISTS ix_lan_tournament_participants_user_id
    ON lan_tournament_participants(user_id);


-- =================================================================
-- Table 4: lan_tournament_matches (match records)
-- =================================================================
CREATE TABLE IF NOT EXISTS lan_tournament_matches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tournament_id UUID NOT NULL REFERENCES lan_tournaments(id),
    group_order INTEGER,
    match_order INTEGER,
    confirmed_by UUID REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_lan_tournament_matches_tournament_id
    ON lan_tournament_matches(tournament_id);


-- =================================================================
-- Table 5: lan_tournament_match_contestants (match participants)
-- =================================================================
CREATE TABLE IF NOT EXISTS lan_tournament_match_contestants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tournament_match_id UUID NOT NULL REFERENCES lan_tournament_matches(id),
    team_id UUID REFERENCES lan_tournament_teams(id),
    participant_id UUID REFERENCES lan_tournament_participants(id),
    score INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT ck_exactly_one_contestant CHECK (
        (team_id IS NOT NULL AND participant_id IS NULL) OR
        (team_id IS NULL AND participant_id IS NOT NULL)
    ),
    CONSTRAINT ck_score_non_negative CHECK (score IS NULL OR score >= 0)
);

CREATE INDEX IF NOT EXISTS ix_lan_tournament_match_contestants_match_id
    ON lan_tournament_match_contestants(tournament_match_id);


-- =================================================================
-- Table 6: lan_tournament_match_comments (match discussion)
-- =================================================================
CREATE TABLE IF NOT EXISTS lan_tournament_match_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tournament_match_id UUID NOT NULL REFERENCES lan_tournament_matches(id),
    created_by UUID NOT NULL REFERENCES users(id),
    comment TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_lan_tournament_match_comments_match_id
    ON lan_tournament_match_comments(tournament_match_id);

COMMIT;
