BEGIN;

ALTER TABLE pizza_delivery_entries
    ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP;

-- Delivered rows keep their call time: updated_at is when they were
-- last touched, which for a delivered entry is the delivery in all but
-- rare cases (a later user link).
UPDATE pizza_delivery_entries
   SET delivered_at = updated_at
 WHERE status = 'delivered'
   AND delivered_at IS NULL;

COMMIT;
