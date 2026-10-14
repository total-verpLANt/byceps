BEGIN;

-- Delivered entries are hard-deleted after pickup. This table keeps the
-- per-party number of such deleted entries, so "pizzas consumed" survives
-- the deletion. There is no backfill: entries deleted before this
-- migration are gone. To seed a party by hand:
--   INSERT INTO pizza_delivery_party_counters (party_id, deleted_delivered_count)
--   VALUES ('<party_id>', <n>)
--   ON CONFLICT (party_id) DO UPDATE SET deleted_delivered_count = EXCLUDED.deleted_delivered_count;
CREATE TABLE IF NOT EXISTS pizza_delivery_party_counters (
    party_id TEXT PRIMARY KEY REFERENCES parties(id) ON DELETE CASCADE,
    deleted_delivered_count INTEGER NOT NULL DEFAULT 0
);

COMMIT;
