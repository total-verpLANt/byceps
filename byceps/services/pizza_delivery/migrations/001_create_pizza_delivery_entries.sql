CREATE TABLE IF NOT EXISTS pizza_delivery_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    party_id TEXT NOT NULL REFERENCES parties(id),
    number TEXT NOT NULL,
    user_id UUID REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'registered',
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    created_by_id UUID NOT NULL REFERENCES users(id),
    CONSTRAINT uq_pizza_delivery_entries_party_number
        UNIQUE (party_id, number)
);

CREATE INDEX IF NOT EXISTS ix_pizza_delivery_entries_party_id
    ON pizza_delivery_entries(party_id);
