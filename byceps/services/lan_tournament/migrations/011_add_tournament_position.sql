-- Add position column for controlling tournament display order per party
ALTER TABLE lan_tournaments ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
