CREATE TABLE IF NOT EXISTS party_ticket_chair_optouts (
    id UUID PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id),
    ticket_id UUID NOT NULL REFERENCES tickets(id),
    user_id UUID NOT NULL REFERENCES users(id),
    brings_own_chair BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_party_ticket_chair_optouts_party_id_ticket_id
        UNIQUE (party_id, ticket_id)
);

CREATE INDEX IF NOT EXISTS ix_party_ticket_chair_optouts_party_id
    ON party_ticket_chair_optouts(party_id);
CREATE INDEX IF NOT EXISTS ix_party_ticket_chair_optouts_ticket_id
    ON party_ticket_chair_optouts(ticket_id);
CREATE INDEX IF NOT EXISTS ix_party_ticket_chair_optouts_user_id
    ON party_ticket_chair_optouts(user_id);
