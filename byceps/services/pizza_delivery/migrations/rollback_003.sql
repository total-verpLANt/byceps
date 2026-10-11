BEGIN;

ALTER TABLE pizza_delivery_entries
    DROP COLUMN IF EXISTS delivered_at;

COMMIT;
