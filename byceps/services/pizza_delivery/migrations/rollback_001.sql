-- =================================================================
-- Rollback: 001_create_pizza_delivery_entries
-- =================================================================
-- WARNING: This will DELETE all pizza delivery data!
--
-- This rollback script removes the pizza_delivery_entries table
-- and all its data. Only run this if the migration needs to be
-- undone or in development/testing.
--
-- IMPORTANT: Ensure you have a database backup before running!
--
-- To create backup:
--   pg_dump -U byceps byceps | gzip > backup-$(date +%Y%m%d-%H%M%S).sql.gz
--
-- =================================================================

BEGIN;

DROP INDEX IF EXISTS ix_pizza_delivery_entries_party_id;
DROP TABLE IF EXISTS pizza_delivery_entries CASCADE;

COMMIT;
