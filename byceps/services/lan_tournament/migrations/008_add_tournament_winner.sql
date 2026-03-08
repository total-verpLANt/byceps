ALTER TABLE lan_tournaments
    ADD COLUMN winner_team_id UUID NULL
        REFERENCES lan_tournament_teams (id),
    ADD COLUMN winner_participant_id UUID NULL
        REFERENCES lan_tournament_participants (id);
