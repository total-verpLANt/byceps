-- Allow a round robin group phase with one group to lead into playoffs.
BEGIN;

-- Serialize with concurrent migration runs and writes.
LOCK TABLE lan_tournaments IN ACCESS EXCLUSIVE MODE;

-- Widening cannot fail on existing rows; drop and re-add is idempotent.
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
            AND playoff_group_count >= 1
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
