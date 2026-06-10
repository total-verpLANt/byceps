-- Reverse: drop FFA columns, restore tournament_mode

ALTER TABLE lan_tournaments
    DROP CONSTRAINT IF EXISTS ck_valid_mode_combination;

ALTER TABLE lan_tournament_match_contestants
    DROP COLUMN IF EXISTS contestant_status,
    DROP COLUMN IF EXISTS points,
    DROP COLUMN IF EXISTS placement;

ALTER TABLE lan_tournaments
    DROP COLUMN IF EXISTS points_carry_to_losers,
    DROP COLUMN IF EXISTS group_size_max,
    DROP COLUMN IF EXISTS group_size_min,
    DROP COLUMN IF EXISTS advancement_count,
    DROP COLUMN IF EXISTS point_table;

-- Restore tournament_mode from game_format + elimination_mode
ALTER TABLE lan_tournaments ADD COLUMN tournament_mode TEXT NULL;

UPDATE lan_tournaments SET
    tournament_mode = CASE
        WHEN game_format = 'HIGHSCORE' THEN 'HIGHSCORE'
        ELSE elimination_mode  -- ONE_V_ONE maps directly to elimination_mode name
    END
WHERE game_format IS NOT NULL;

ALTER TABLE lan_tournaments
    DROP COLUMN IF EXISTS elimination_mode,
    DROP COLUMN IF EXISTS game_format;
