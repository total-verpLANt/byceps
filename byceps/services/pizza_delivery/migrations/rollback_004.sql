-- =================================================================
-- Rollback: 004_add_party_counters
-- =================================================================
-- WARNING: This DELETES the per-party counts of delivered pizza
-- delivery entries that were already deleted. They cannot be rebuilt
-- from pizza_delivery_entries.
--
-- IMPORTANT: Ensure you have a database backup before running!
-- =================================================================

BEGIN;

DROP TABLE IF EXISTS pizza_delivery_party_counters;

COMMIT;
