-- Additive readiness repair. Human approval/backup required before deployment.
-- IDs normally come from application uuid7 defaults (not server defaults).
-- Historical backfill uses PostgreSQL-generated UUIDs; it invents no Ready facts.
BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE lan_tournament_matches
    ADD COLUMN IF NOT EXISTS pairing_generation BIGINT NOT NULL DEFAULT '0',
    ADD COLUMN IF NOT EXISTS readiness_revision BIGINT NOT NULL DEFAULT '0',
    ADD COLUMN IF NOT EXISTS pairing_id UUID NULL,
    ADD COLUMN IF NOT EXISTS invitation_hold_a BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS invitation_hold_b BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS lan_tournament_match_pairings (
    id UUID PRIMARY KEY,
    match_id UUID NOT NULL,
    tournament_id UUID NOT NULL,
    generation BIGINT NOT NULL,
    side_a_kind VARCHAR(11) NOT NULL,
    side_b_kind VARCHAR(11) NOT NULL,
    side_a_id UUID NOT NULL,
    side_b_id UUID NOT NULL,
    started_at TIMESTAMP NULL,
    ended_at TIMESTAMP NULL
);

CREATE TABLE IF NOT EXISTS lan_tournament_match_invitations (
    id UUID PRIMARY KEY,
    match_id UUID NOT NULL,
    tournament_id UUID NOT NULL,
    pairing_generation BIGINT NOT NULL,
    recipient_id UUID NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    dispatch_token UUID NULL,
    expected_readiness_revision BIGINT NOT NULL,
    next_attempt_at TIMESTAMP NULL,
    lease_until TIMESTAMP NULL,
    accepted_at TIMESTAMP NULL,
    last_error VARCHAR(500) NULL
);

-- PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS. Scope every guard to its
-- relation, so identically named constraints in unrelated schemas do not count.
DO $migration$
DECLARE
    item RECORD;
BEGIN
    FOR item IN SELECT * FROM (VALUES
        ('lan_tournament_matches', 'ck_lan_tournament_matches_pairing_generation', 'CHECK (pairing_generation >= 0)'),
        ('lan_tournament_matches', 'ck_lan_tournament_matches_readiness_revision', 'CHECK (readiness_revision >= 0)'),
        ('lan_tournament_matches', 'fk_lan_tournament_matches_pairing_id', 'FOREIGN KEY (pairing_id) REFERENCES lan_tournament_match_pairings (id)'),
        ('lan_tournament_match_pairings', 'uq_lan_tournament_match_pairings_match_generation', 'UNIQUE (match_id, generation)'),
        ('lan_tournament_match_pairings', 'ck_lan_tournament_match_pairings_generation', 'CHECK (generation >= 0)'),
        ('lan_tournament_match_pairings', 'ck_lan_tournament_match_pairings_side_a_kind', $$CHECK (side_a_kind IN ('participant', 'team'))$$),
        ('lan_tournament_match_pairings', 'ck_lan_tournament_match_pairings_side_b_kind', $$CHECK (side_b_kind IN ('participant', 'team'))$$),
        ('lan_tournament_match_pairings', 'ck_lan_tournament_match_pairings_distinct_sides', 'CHECK (side_a_kind <> side_b_kind OR side_a_id <> side_b_id)'),
        ('lan_tournament_match_pairings', 'ck_lan_tournament_match_pairings_time_order', 'CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at)'),
        ('lan_tournament_match_invitations', 'uq_lan_tournament_match_invitations_match_generation_recipient', 'UNIQUE (match_id, pairing_generation, recipient_id)'),
        ('lan_tournament_match_invitations', 'fk_lan_tournament_match_invitations_recipient_id', 'FOREIGN KEY (recipient_id) REFERENCES users (id)'),
        ('lan_tournament_match_invitations', 'ck_lan_tournament_match_invitations_pairing_generation', 'CHECK (pairing_generation >= 0)'),
        ('lan_tournament_match_invitations', 'ck_lan_tournament_match_invitations_readiness_revision', 'CHECK (expected_readiness_revision >= 0)'),
        ('lan_tournament_match_invitations', 'ck_lan_tournament_match_invitations_attempts', 'CHECK (attempts >= 0)'),
        ('lan_tournament_match_invitations', 'ck_lan_tournament_match_invitations_status', $$CHECK (status IN ('pending', 'dispatching', 'queued', 'sending', 'accepted', 'failed', 'suppressed', 'delivery_unknown'))$$),
        ('lan_tournament_match_invitations', 'ck_lan_tournament_match_invitations_dispatch_stage', $$CHECK ((status IN ('dispatching', 'queued', 'sending') AND dispatch_token IS NOT NULL AND lease_until IS NOT NULL) OR (status NOT IN ('dispatching', 'queued', 'sending') AND lease_until IS NULL))$$),
        ('lan_tournament_match_invitations', 'ck_lan_tournament_match_invitations_acceptance_stage', $$CHECK ((status = 'accepted' AND accepted_at IS NOT NULL) OR (status <> 'accepted' AND accepted_at IS NULL))$$)
    ) AS definitions(table_name, constraint_name, definition)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = to_regclass(item.table_name)
              AND conname = item.constraint_name
        ) THEN
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s',
                item.table_name, item.constraint_name, item.definition);
        END IF;
    END LOOP;
END;
$migration$;

CREATE INDEX IF NOT EXISTS ix_lan_tournament_match_invitations_status_next_attempt
    ON lan_tournament_match_invitations (status, next_attempt_at);

-- Exactly two real, active, tournament-owned identities, no extra placeholder
-- slots. The effective phase format is authoritative, not the main format.
WITH sides AS (
    SELECT c.tournament_match_id AS match_id, c.created_at, c.id,
        CASE WHEN c.participant_id IS NOT NULL THEN 'participant' ELSE 'team' END AS kind,
        COALESCE(c.participant_id, c.team_id) AS contestant_id,
        row_number() OVER (PARTITION BY c.tournament_match_id ORDER BY c.created_at, c.id) AS side
    FROM lan_tournament_match_contestants c
    JOIN lan_tournament_matches m ON m.id = c.tournament_match_id
    LEFT JOIN lan_tournament_participants p ON p.id = c.participant_id
    LEFT JOIN lan_tournament_teams t ON t.id = c.team_id
    WHERE (c.participant_id IS NOT NULL AND c.team_id IS NULL
           AND p.removed_at IS NULL AND p.tournament_id = m.tournament_id)
       OR (c.team_id IS NOT NULL AND c.participant_id IS NULL
           AND t.removed_at IS NULL AND t.tournament_id = m.tournament_id)
), eligible AS (
    SELECT m.*, a.kind AS a_kind, a.contestant_id AS a_id,
        b.kind AS b_kind, b.contestant_id AS b_id,
        CASE WHEN m.occupied_since IS NOT NULL THEN
            GREATEST(m.occupied_since, a.created_at, b.created_at)
        ELSE NULL END AS known_started_at
    FROM lan_tournament_matches m
    JOIN lan_tournaments t ON t.id = m.tournament_id
    JOIN sides a ON a.match_id = m.id AND a.side = 1
    JOIN sides b ON b.match_id = m.id AND b.side = 2
    WHERE m.pairing_id IS NULL
      AND CASE m.phase WHEN 1 THEN t.game_format
          WHEN 2 THEN t.playoff_game_format END = 'ONE_V_ONE'
      AND (SELECT count(*) FROM sides s WHERE s.match_id = m.id) = 2
      AND (SELECT count(*) FROM lan_tournament_match_contestants c WHERE c.tournament_match_id = m.id) = 2
      AND (a.kind <> b.kind OR a.contestant_id <> b.contestant_id)
)
INSERT INTO lan_tournament_match_pairings
    (id, match_id, tournament_id, generation, side_a_kind, side_a_id,
     side_b_kind, side_b_id, started_at)
SELECT gen_random_uuid(), id, tournament_id, pairing_generation,
    a_kind, a_id, b_kind, b_id, known_started_at
FROM eligible ORDER BY created_at, id
ON CONFLICT (match_id, generation) DO NOTHING;

UPDATE lan_tournament_matches m SET pairing_id = p.id
FROM lan_tournament_match_pairings p
WHERE m.pairing_id IS NULL AND p.match_id = m.id
  AND p.generation = m.pairing_generation AND p.ended_at IS NULL;

-- Legacy assignment mail has no recipient acceptance evidence. Never infer
-- success from both_ready_notified_at, or resend the historical audience.
-- Pre-start tournaments have no work yet: actual start activates pending work.
WITH audience AS (
    SELECT p.match_id, u.user_id
    FROM lan_tournament_match_pairings p
    JOIN lan_tournament_matches m ON m.pairing_id = p.id
    JOIN lan_tournaments t ON t.id = m.tournament_id
    JOIN lan_tournament_participants u ON u.tournament_id = t.id
        AND u.removed_at IS NULL
        AND ((p.side_a_kind = 'participant' AND u.id = p.side_a_id)
          OR (p.side_b_kind = 'participant' AND u.id = p.side_b_id)
          OR (p.side_a_kind = 'team' AND u.team_id = p.side_a_id)
          OR (p.side_b_kind = 'team' AND u.team_id = p.side_b_id))
    WHERE t.tournament_status = 'ONGOING' AND m.confirmed_by IS NULL
)
INSERT INTO lan_tournament_match_invitations
    (id, match_id, tournament_id, pairing_generation, recipient_id, status,
     expected_readiness_revision)
SELECT gen_random_uuid(), m.id, m.tournament_id, m.pairing_generation,
    a.user_id, 'delivery_unknown', m.readiness_revision
FROM (SELECT DISTINCT match_id, user_id FROM audience) a
JOIN lan_tournament_matches m ON m.id = a.match_id
ORDER BY m.created_at, m.id, a.user_id
ON CONFLICT (match_id, pairing_generation, recipient_id) DO NOTHING;

COMMIT;
