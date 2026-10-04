-- Intentional one-time reset for the local test runtime, not an installation step.
-- Back up the database and stop application/worker processes before running.
BEGIN;

LOCK TABLE tickets IN ACCESS EXCLUSIVE MODE;

DO $reset$
DECLARE
    legacy_table regclass := to_regclass('party_ticket_chair_optouts');
    legacy_columns text[];
BEGIN
    IF legacy_table IS NOT NULL THEN
        EXECUTE 'LOCK TABLE party_ticket_chair_optouts IN ACCESS EXCLUSIVE MODE';
        SELECT array_agg(attname::text ORDER BY attname)
        INTO legacy_columns
        FROM pg_attribute
        WHERE attrelid = legacy_table AND attnum > 0 AND NOT attisdropped;

        IF legacy_columns IS DISTINCT FROM ARRAY[
            'brings_own_chair', 'id', 'party_id', 'ticket_id', 'updated_at', 'user_id'
        ]::text[] THEN
            RAISE EXCEPTION 'Unexpected legacy chair table columns: %', legacy_columns;
        END IF;

        -- RESTRICT is intentional: unexpected dependencies abort the reset.
        DROP TABLE party_ticket_chair_optouts;
    END IF;
END
$reset$;

UPDATE tickets SET chair_source = 'unknown'
WHERE chair_source IS DISTINCT FROM 'unknown';

COMMIT;
