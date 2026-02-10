-- =================================================================
-- Rollback: 001_create_lan_tournament_schema
-- =================================================================
-- WARNING: This will DELETE all tournament data!
--
-- This rollback script removes all LAN tournament tables and their data.
-- Only run this if the migration needs to be undone or in development/testing.
--
-- IMPORTANT: Ensure you have a database backup before running this script!
--
-- To create backup:
--   pg_dump -U byceps byceps | gzip > backup-$(date +%Y%m%d-%H%M%S).sql.gz
--
-- =================================================================

BEGIN;

-- Drop tables in reverse dependency order (children first, then parents)
-- CASCADE is acceptable here to handle any accidental remaining dependencies

DROP TABLE IF EXISTS lan_tournament_match_comments CASCADE;
DROP TABLE IF EXISTS lan_tournament_match_contestants CASCADE;
DROP TABLE IF EXISTS lan_tournament_matches CASCADE;
DROP TABLE IF EXISTS lan_tournament_participants CASCADE;
DROP TABLE IF EXISTS lan_tournament_teams CASCADE;
DROP TABLE IF EXISTS lan_tournaments CASCADE;

COMMIT;
