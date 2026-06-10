-- Mode composition: split tournament_mode into game_format + elimination_mode
-- Also adds FFA tournament support columns

-- Step 1: Add new mode columns
ALTER TABLE lan_tournaments
    ADD COLUMN game_format TEXT NULL,
    ADD COLUMN elimination_mode TEXT NULL;

-- Step 2: Migrate existing tournament_mode data
UPDATE lan_tournaments SET
    game_format = CASE tournament_mode
        WHEN 'SINGLE_ELIMINATION' THEN 'ONE_V_ONE'
        WHEN 'DOUBLE_ELIMINATION' THEN 'ONE_V_ONE'
        WHEN 'ROUND_ROBIN' THEN 'ONE_V_ONE'
        WHEN 'HIGHSCORE' THEN 'HIGHSCORE'
        ELSE NULL
    END,
    elimination_mode = CASE tournament_mode
        WHEN 'SINGLE_ELIMINATION' THEN 'SINGLE_ELIMINATION'
        WHEN 'DOUBLE_ELIMINATION' THEN 'DOUBLE_ELIMINATION'
        WHEN 'ROUND_ROBIN' THEN 'ROUND_ROBIN'
        WHEN 'HIGHSCORE' THEN 'NONE'
        ELSE NULL
    END
WHERE tournament_mode IS NOT NULL;

-- Step 3: Drop old column
ALTER TABLE lan_tournaments DROP COLUMN tournament_mode;

-- Step 4: Add FFA tournament config columns
ALTER TABLE lan_tournaments
    ADD COLUMN point_table TEXT NULL,
    ADD COLUMN advancement_count INTEGER NULL,
    ADD COLUMN group_size_min INTEGER NULL,
    ADD COLUMN group_size_max INTEGER NULL,
    ADD COLUMN points_carry_to_losers BOOLEAN NULL;

-- Step 5: Add FFA match contestant columns
ALTER TABLE lan_tournament_match_contestants
    ADD COLUMN placement INTEGER NULL,
    ADD COLUMN points INTEGER NULL,
    ADD COLUMN contestant_status TEXT NULL;

-- Step 6: Enforce valid mode combinations at DB level
ALTER TABLE lan_tournaments
    ADD CONSTRAINT ck_valid_mode_combination CHECK (
        (game_format = 'ONE_V_ONE'
            AND elimination_mode IN ('SINGLE_ELIMINATION','DOUBLE_ELIMINATION','ROUND_ROBIN'))
        OR (game_format = 'FREE_FOR_ALL'
            AND elimination_mode IN ('SINGLE_ELIMINATION','DOUBLE_ELIMINATION'))
        OR (game_format = 'HIGHSCORE' AND elimination_mode = 'NONE')
        OR (game_format IS NULL AND elimination_mode IS NULL)
    );
