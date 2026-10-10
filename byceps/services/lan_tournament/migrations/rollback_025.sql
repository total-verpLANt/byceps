-- Restore the two-group minimum for round robin playoffs.
-- Refuses while any tournament uses one playoff group; change them first.
BEGIN;

LOCK TABLE lan_tournaments IN ACCESS EXCLUSIVE MODE;

-- Concatenate instead of a RAISE format: a bare percent sign breaks drivers
-- that treat it as a parameter placeholder.
DO $$
DECLARE
    one_group_count bigint;
BEGIN
    SELECT count(*) INTO one_group_count
    FROM lan_tournaments WHERE playoff_group_count = 1;
    IF one_group_count > 0 THEN
        RAISE EXCEPTION USING MESSAGE = 'rollback_025: ' || one_group_count
            || ' tournament(s) use one playoff group; change them first';
    END IF;
END $$;

ALTER TABLE lan_tournaments
    DROP CONSTRAINT IF EXISTS ck_lan_tournaments_playoff_config;

ALTER TABLE lan_tournaments
    ADD CONSTRAINT ck_lan_tournaments_playoff_config CHECK (
        (
            playoff_game_format IS NULL
            AND playoff_elimination_mode IS NULL
            AND playoff_group_count IS NULL
            AND playoff_qualifiers_per_group IS NULL
            AND playoff_qualifier_count IS NULL
            AND playoff_release_mode IS NULL
        ) OR COALESCE((
            game_format = 'ONE_V_ONE'
            AND elimination_mode = 'ROUND_ROBIN'
            AND playoff_game_format = 'ONE_V_ONE'
            AND playoff_elimination_mode IN
                ('SINGLE_ELIMINATION', 'DOUBLE_ELIMINATION')
            AND playoff_group_count >= 2
            AND playoff_qualifiers_per_group >= 1
            AND playoff_qualifier_count IS NULL
            AND playoff_release_mode IS NOT NULL
        ), FALSE) OR COALESCE((
            game_format = 'HIGHSCORE'
            AND playoff_game_format = 'FREE_FOR_ALL'
            AND playoff_elimination_mode IN
                ('SINGLE_ELIMINATION', 'DOUBLE_ELIMINATION')
            AND playoff_qualifier_count >= 2
            AND playoff_group_count IS NULL
            AND playoff_qualifiers_per_group IS NULL
            AND playoff_release_mode IS NOT NULL
        ), FALSE)
    );

COMMIT;
