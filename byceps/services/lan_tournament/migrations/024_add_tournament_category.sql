-- F01: category and request provenance are independent.
BEGIN;

-- Serialize first-time detection with concurrent migration runs and writes.
LOCK TABLE lan_tournaments IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'lan_tournaments'::regclass
          AND attname = 'category' AND NOT attisdropped
    ) THEN
        ALTER TABLE lan_tournaments
            ADD COLUMN category TEXT NOT NULL DEFAULT 'MAIN';
        -- Backfill only when adding the column. Re-runs preserve admin choices.
        UPDATE lan_tournaments SET category = 'USER_ORGANIZED'
            WHERE created_from_request_id IS NOT NULL;
    END IF;
END $$;

ALTER TABLE lan_tournaments ALTER COLUMN category SET DEFAULT 'MAIN';
ALTER TABLE lan_tournaments ALTER COLUMN category SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'lan_tournaments'::regclass
          AND conname = 'ck_lan_tournaments_category'
    ) THEN
        ALTER TABLE lan_tournaments ADD CONSTRAINT ck_lan_tournaments_category
            CHECK (category IN ('MAIN', 'FUN', 'STAGE', 'USER_ORGANIZED'));
    END IF;
END $$;

COMMIT;
