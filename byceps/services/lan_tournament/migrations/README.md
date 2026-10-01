# LAN Tournament Database Migrations

This directory contains database migration scripts for the BYCEPS LAN tournament module.

## Overview

BYCEPS uses **manual database migrations** - there is no automated migration tool like Alembic or Flyway. Database administrators apply SQL scripts manually after reviewing changes and creating backups.

This approach provides:
- Full control over schema changes
- Explicit review of all database modifications
- Clear audit trail of when and how changes were applied
- Predictable deployment process for production LAN parties

## BYCEPS Convention: No Database CASCADE

**CRITICAL:** All migrations in this module follow BYCEPS convention of **NO database-level cascade behaviors** (`ON DELETE CASCADE` or `ON DELETE SET NULL`).

### Why No CASCADE?

All foreign keys use the default `ON DELETE NO ACTION` behavior. Cleanup of dependent entities is handled **explicitly at the application service layer**:

- **Full audit trail**: Every deletion is logged with domain events
- **Business logic control**: Application decides deletion order and rules
- **Event emission**: Signals notify other parts of system of deletions
- **Transaction safety**: Service layer controls transaction boundaries
- **No accidental data loss**: Database won't silently cascade deletes

### Application-Level CASCADE Implementation

Deletion operations are handled in service layer:

- `tournament_service.py::delete_tournament()` - Deletes the tournament and its dependencies; log entries are kept, and a `tournament-deleted` entry is written
- `tournament_team_service.py::delete_team()` - Removes team references, then deletes team
- `tournament_match_service.py::delete_match()` - Deletes match with comments and contestants

See `/workspace/tests/unit/services/lan_tournament/test_tournament_deletion.py` for comprehensive tests of CASCADE behavior.

## Available Migrations

### 001_create_lan_tournament_schema.sql

Creates the complete LAN tournament schema with 6 tables:

1. **lan_tournaments** - Main tournament registry
2. **lan_tournament_teams** - Team definitions
3. **lan_tournament_participants** - Player registrations
4. **lan_tournament_matches** - Match records
5. **lan_tournament_match_contestants** - Match participants (teams or individuals)
6. **lan_tournament_match_comments** - Match discussion

**Features:**
- Transaction-wrapped (atomic apply)
- Idempotent (`IF NOT EXISTS` clauses)
- All CHECK constraints for data validation
- All UNIQUE constraints for data integrity
- All indexes for query performance
- Zero CASCADE behaviors (BYCEPS convention)

### 003_add_soft_delete_columns.sql

Adds soft-delete support to participants and teams:

1. **`removed_at` column** on `lan_tournament_participants` — nullable timestamp marking soft-deleted rows
2. **`removed_at` column** on `lan_tournament_teams` — same for teams
3. **Tightened constraint** `ck_exactly_one_contestant` — replaces `ck_at_most_one_contestant` so match contestants always reference exactly one team or participant
4. **Partial indexes** `ix_lan_tournament_participants_active` and `ix_lan_tournament_teams_active` — efficient lookup of active (non-deleted) rows

**Why soft-delete?** During ONGOING tournaments, participants/teams removed (e.g. ticketless) must keep their rows so that `lan_tournament_match_contestants` foreign keys remain valid. The service layer re-joins soft-deleted participants by clearing `removed_at` instead of inserting a new row, avoiding `UniqueConstraint('tournament_id', 'user_id')` conflicts.

**Rollback:** `rollback_003.sql`

### 012_add_log_entries.sql

Creates the `lan_tournament_log_entries` audit log table:

1. **`id UUID`** primary key — application-generated uuid7
2. **`occurred_at TIMESTAMPTZ NOT NULL`** — when the event happened, indexed via `ix_lan_tournament_log_entries_occurred_at` for the retention purge
3. **`event_type TEXT NOT NULL`** — event discriminator string
4. **`tournament_id UUID NOT NULL`** — FK to `lan_tournaments.id`, indexed via `ix_lan_tournament_log_entries_tournament_id`
5. **`initiator_id UUID NULL`** — nullable FK to `users.id` (system-triggered entries have no initiator)
6. **`data JSONB NOT NULL DEFAULT '{}'::jsonb`** — structured payload

Shape mirrors the stock tourney log table. Zero CASCADE behaviors (BYCEPS convention).

**Rollback:** `rollback_012.sql` (drops both indexes, then table)

**Retention:** `byceps purge-lan-tournament-log-entries --older-than-days N [--dry-run]` (default 365 days) hard-deletes older entries; `--dry-run` only reports the count.

### 013_drop_log_entry_tournament_fk.sql

Drops `fk_lan_tournament_log_entries_tournament_id`; the `tournament_id` column, its `NOT NULL` constraint and its index stay.

**Why:** with the FK, deleting a tournament had to delete its log entries first, so the audited role could erase its own trail. Entries now outlive their tournament; only the retention purge removes them.

**Rollback:** `rollback_013.sql` — re-adds the FK. It fails once any tournament has been deleted since 013, and it does not delete the orphaned entries to force it through.

### 014_add_tournament_orga.sql

Creates the `lan_tournament_orgas` table, recording which users are assigned as organizers ("orgas") of a given tournament:

1. **`id UUID`** primary key — application-generated uuid7
2. **`tournament_id UUID NOT NULL`** — FK to `lan_tournaments.id`, indexed via `ix_lan_tournament_orgas_tournament_id`
3. **`user_id UUID NOT NULL`** — FK to `users.id`, indexed via `ix_lan_tournament_orgas_user_id`
4. **`assigned_at TIMESTAMPTZ NOT NULL`** — when the assignment was made
5. **`assigned_by_id UUID NULL`** — nullable FK to `users.id` (fixture- and system-created assignments have no initiator)
6. **`duties TEXT NULL`** — optional free-text description of the orga's responsibilities

`uq_lan_tournament_orgas_tournament_user` — `UNIQUE (tournament_id, user_id)` — mirrors `DbMembership.__table_args__` in `orga_team/dbmodels.py` and guards against a double-submit of the assign form creating two rows for the same person. Zero CASCADE behaviors (BYCEPS convention).

The readiness migration on `prd/f04-match-ready` is numbered 021 after its
rebase onto `prd/fixes`; this orga migration and rollback remain numbered 014.

**Rollback:** `rollback_014.sql` (drops both indexes, then table)

#### Required follow-up: apply migration 015

This migration ships with a NEW permission, `lan_tournament.orga_assign`.
Permissions are registered in code (`permissions.py`), but the mapping from a
role to its permissions lives in the database, so deploying the code alone
grants it to nobody. Until it is granted, the "Assign orga" form does not
render, `POST .../orgas/assign` and `.../orgas/<user_id>/revoke` answer `403`,
and no tournament orga can be appointed — the whole feature is inert while
looking merely empty. Observed on staging after this branch was deployed.

**Apply `015_grant_orga_assign_permission.sql` after this one.** Do not reach
for `import-roles`: it is create-only and cannot add a permission to a role
that already exists (it reports `Imported 0 roles, skipped 2 roles` and changes
nothing). 015's header explains why, quoting the `continue` in
`impex_service._create_roles()` that skips the assignment loop.


### 015_grant_orga_assign_permission.sql

Data-only; no schema change. Grants `lan_tournament.orga_assign` to every role
that already holds `lan_tournament.administrate`, plus the canonical
`lan_tournament_admin` role by name. Data-driven rather than hardcoded, because
a deployment need not use the role from `lan_tournament_roles.toml` — staging
does not — and naming one role would silently fix nothing on the installations
that need it most.

Idempotent via `ON CONFLICT DO NOTHING` against the
`(role_id, permission_id)` primary key, so a re-run is a no-op and an install
already granted by hand is left alone. Transaction-wrapped.

Note: BYCEPS resolves a session's permissions on every request, not at login
(`byceps/util/user_session.py::get_current_user`, called from each
blueprint's `before_app_request`). The grant takes effect on the very next
request — no re-login needed.

Note: check the highest migration number in the integration base and sibling
branches before adding another migration.

**Rollback:** `rollback_015.sql` — revokes the permission from every role that
holds it. Not a precise undo: after `ON CONFLICT DO NOTHING`, a row granted by
015 is indistinguishable from one granted by hand beforehand, so record the
current grants first if any were deliberate. Existing rows in
`lan_tournament_orgas` are untouched — already-appointed orgas keep their
scoped site-side rights, which are checked against that table rather than this
permission.

### 016_add_tournament_requests.sql

Creates the `lan_tournament_requests` table, the user-facing propose-a-
tournament queue (PRD §22), and adds `lan_tournaments.created_from_request_id`
linking an accepted request to the tournament it produced:

1. **`id UUID`** primary key — application-generated uuid7
2. **`party_id TEXT NOT NULL`** — FK to `parties.id`, indexed via
   `ix_lan_tournament_requests_party_id`
3. **`number INTEGER NOT NULL`** — sequential per-party request number (e.g.
   `#0142`), allocated as `MAX(number)+1`; `ck_lan_tournament_requests_number`
   requires it positive
4. **`uq_lan_tournament_requests_party_number`** — `UNIQUE (party_id, number)`
   backs that per-party sequence
5. **`proposer_id UUID NOT NULL`** — FK to `users.id`, indexed via
   `ix_lan_tournament_requests_proposer_id`
6. **`status TEXT NOT NULL`** — one of the five request statuses, indexed via
   `ix_lan_tournament_requests_status` for the admin queue filter
7. **`created_at TIMESTAMPTZ NOT NULL`** / **`updated_at TIMESTAMPTZ NULL`**
8. **`name`, `game`, `game_format`, `elimination_mode`, `description`** — all
   `TEXT NOT NULL`; **`team_size INTEGER NOT NULL`**
   (`ck_lan_tournament_requests_team_size` requires `>= 1`) and
   **`participant_limit INTEGER NOT NULL`**
   (`ck_lan_tournament_requests_limit` requires `>= 2`)
9. **`preferred_start_time` / `preferred_end_time TIMESTAMPTZ NOT NULL`** —
   `ck_lan_tournament_requests_period` requires the end at or after the start
10. **`special_rules`, `notes`, `desired_template`** — all `TEXT NULL`
11. **`decided_at TIMESTAMPTZ NULL`**, **`decided_by_id UUID NULL`** (FK to
    `users.id`), **`rejection_reason TEXT NULL`** —
    `ck_lan_tournament_requests_rejection_reason` requires a reason once
    `status = 'rejected'`
12. **`created_tournament_id UUID NULL`** — FK to `lan_tournaments.id`, set
    once the request is accepted and a tournament created from it
13. **`ix_lan_tournament_requests_created_tournament_id`** — partial index on
    `created_tournament_id` (`WHERE created_tournament_id IS NOT NULL`); backs
    `unlink_created_tournament_flush`'s lookup by created tournament during
    `delete_tournament`'s cascade, and the
    `fk_lan_tournament_requests_created_tournament_id` FK check that Postgres
    runs against this table on every `lan_tournaments` row delete

The inverse link, `lan_tournaments.created_from_request_id UUID NULL`, is
guarded by the partial unique index
`uq_lan_tournaments_created_from_request_id` (`WHERE created_from_request_id
IS NOT NULL`) — the PRD §26 idempotency guard: a second create from the same
request cannot produce a second tournament. Zero CASCADE behaviors (BYCEPS
convention).

Note: the branch `prd/f15-dispute-review` also uses 016
(`016_add_match_dispute_and_score_history.sql`). This collision is a deliberate,
accepted decision -- whichever of the two branches is merged second must
renumber its migration, its rollback and its README entry before merging.

**Rollback:** `rollback_016.sql` (drops the partial unique index, the
`created_from_request_id` column, all four request indexes, then deletes every
`lan_tournament_log_entries` row whose `event_type` starts with
`'tournament-request-'` before dropping the table — irreversible: all request
audit history is lost)

Note: `ix_lan_tournament_requests_created_tournament_id` was added to this file
after its first commit; an operator who already applied an earlier version
should re-run 016 — every statement is idempotent, so the re-run only adds
the missing index and changes nothing else.

Note: `rollback_016.sql` requires PostgreSQL 11 or higher (it calls
`starts_with()`).

#### Required follow-up: apply migration 017

This migration's feature ships with TWO new permissions,
`lan_tournament.request_view` and `lan_tournament.request_decide`
(`permissions.py`). Permissions are registered in code but the mapping
from a role to its permissions lives in the database, so deploying the
code alone grants them to nobody. Until 017 is applied, every request
admin route (queue, detail, accept, reject, edit) answers `403` and the
nav tab is hidden, while participants can keep submitting requests
through the site surface.

**Apply `017_grant_tournament_request_permissions.sql` after this
one.** Do not reach for `import-roles`: it is create-only and cannot
add a permission to a role that already exists. See 017's header for
the fuller writeup (the same reasoning as 015, for `orga_assign`).

### 017_grant_tournament_request_permissions.sql

Data-only; no schema change. Grants both `lan_tournament.request_view`
and `lan_tournament.request_decide` to every role that already holds
`lan_tournament.administrate`. Data-driven rather than hardcoded, because
a deployment need not use the role from `lan_tournament_roles.toml` —
staging does not — and naming one role would silently fix nothing on
the installations that need it most. `lan_tournament_viewer` gets
neither permission: it is read-only and request review is a privileged
action.

There is no `authz_permissions` table to populate first —
`authz_role_permissions.permission_id` carries no foreign key;
permissions exist only as strings registered in code at app start.

Idempotent via `ON CONFLICT DO NOTHING` against the
`(role_id, permission_id)` primary key, so a re-run is a no-op and an
install already granted by hand is left alone. Transaction-wrapped.

Note: BYCEPS resolves a session's permissions on every request, not at
login (`byceps/util/user_session.py::get_current_user`, called from
each blueprint's `before_app_request`). The grant takes effect on the
very next request — no re-login needed.

Note: the branch `prd/f05-dq-reasons` also uses 017. This collision is
a deliberate, accepted decision -- whichever of the two branches is
merged second must renumber its migration, its rollback and this
README entry before merging.

**Rollback:** `rollback_017.sql` — revokes both permissions from every
role that holds either one. Not a precise undo: after
`ON CONFLICT DO NOTHING`, a row granted by 017 is indistinguishable
from one granted by hand beforehand, so record the current grants
first if any were deliberate. Every other grant, including
`lan_tournament.administrate` itself, is untouched.

### 018_add_tournament_images.sql

Creates the `lan_tournament_images` table, the uploaded cover images of the
admin create wizard, and adds `lan_tournaments.image_id`, `image_alt_text` and
`creation_token`:

1. **`id UUID`** primary key -- application-generated uuid7
2. **`party_id TEXT NOT NULL`** -- FK to `parties.id`; leads the composite
   index `ix_lan_tournament_images_party_id_created_at` (`party_id,
   created_at`), which backs the per-party picker and the lookup of old
   unreferenced images
3. **`creator_id UUID NOT NULL`** -- FK to `users.id`, the uploader
4. **`created_at TIMESTAMPTZ NOT NULL`**
5. **`filename TEXT NOT NULL`** -- display data only;
   `ck_lan_tournament_images_filename_length` requires 1 to 200 characters
6. **`image_type TEXT NOT NULL`** -- `ImageType` member name in lower case;
   `ck_lan_tournament_images_image_type` allows `jpeg`, `png` and `webp`
7. **`width` / `height INTEGER NOT NULL`** --
   `ck_lan_tournament_images_dimensions` requires both positive
8. **`byte_size INTEGER NOT NULL`** -- `ck_lan_tournament_images_byte_size`
   requires it positive

On `lan_tournaments`:

- **`image_id UUID NULL`** -- FK `fk_lan_tournaments_image_id` to
  `lan_tournament_images.id`, indexed via `ix_lan_tournaments_image_id`
  (backs the reference check on image deletion)
- **`image_alt_text TEXT NULL`** -- per-tournament alt text; empty or NULL
  means decorative
- **`creation_token UUID NULL`** -- idempotency token of a create, guarded by
  the partial unique index `uq_lan_tournaments_creation_token` (`WHERE
  creation_token IS NOT NULL`), so a double submit cannot produce a second
  tournament

The foreign key is added inside a `DO $$` block that checks `pg_constraint`
by name: PostgreSQL has no `ADD CONSTRAINT IF NOT EXISTS`, and this is the
first idempotent `ADD CONSTRAINT` in this module. Zero CASCADE behaviors
(BYCEPS convention).

Note: the branch `prd/f07-waitlist` also uses 018
(`018_add_waitlist.sql`). This collision is a deliberate, accepted decision --
whichever of the two branches is merged second must renumber its migration,
its rollback and its README entry before merging.

Note: the purge-on-upload of abandoned images (decision D2, mentioned in the
header comment of `018_add_tournament_images.sql`) was replaced by the manual
Wartung tab on 2026-09-30. Uploading no longer deletes anything.

The same file also grants `lan_tournament.maintain` (Wartung tab) to every
role that holds `lan_tournament.administrate`: data-driven, no permission
table, `ON CONFLICT DO NOTHING`, so it is idempotent and takes effect on the
next request. `import-roles` is create-only and would not apply it to existing
roles. Staging/prod already on 018: re-run the updated 018; do not run
rollback_018 (it drops the image table and the image columns).

**Rollback:** `rollback_018.sql` (revokes every `lan_tournament.maintain`
grant -- not a precise undo, it also revokes hand-made grants -- then drops
the token index, the image index, the
FK, the three `lan_tournaments` columns, the images index, then the table --
irreversible: all image rows and every tournament's image link, alt text and
creation token are lost; the files under `data/` stay on disk)

### 019_add_tournament_seedings.sql

Creates the `lan_tournament_seedings` table, the server-held seeding draft of a
LAN tournament (PRD F-10), one row per `(tournament_id, target)`:

1. **`id UUID`** primary key -- application-generated uuid7
2. **`tournament_id UUID NOT NULL`** -- FK `fk_lan_tournament_seedings_tournament_id`
   to `lan_tournaments.id`
3. **`target VARCHAR(40) NOT NULL`** -- `initial`, `playoff` or
   `ffa:<SE|WB|LB>:<round>`, enforced by `ck_lan_tournament_seedings_target`
4. **`seed_code TEXT NOT NULL`** -- the self-contained seed code the orga edits
5. **`version INTEGER NOT NULL DEFAULT 1`** -- optimistic-locking counter;
   `ck_lan_tournament_seedings_version` requires at least 1
6. **`generated_seed_code TEXT NULL`**, **`generated_at TIMESTAMP NULL`** --
   the code the bracket or lobbies were last generated from
7. **`updated_by UUID NULL`** -- FK `fk_lan_tournament_seedings_updated_by` to
   `users.id`
8. **`roster_snapshot JSONB NOT NULL DEFAULT '[]'`** -- `{"id", "label",
   "joined_late"}` entries of the roster the code was built for;
   `joined_late` is optional (absent means false) and marks an entrant a
   re-seed appended, until the next generation; `[]` is valid (legacy drafts)
9. **`created_at` / `updated_at TIMESTAMP NOT NULL`** -- naive, as in the dbmodel

`uq_lan_tournament_seedings_tournament_target` (`tournament_id, target`) backs
every lookup by tournament; there is no further index. No CASCADE behaviors
(BYCEPS convention).

Note: no other `prd/*` branch holds 019 (checked with `git ls-tree` on
2026-09-30). The next migration of this epic is `020_add_playoff_phase.sql`.
If a sibling branch merged first takes 019, renumber this migration, its
rollback and its README entry before merging.

**Rollback:** `rollback_019.sql` (drops the table -- irreversible: all seeding
drafts, generated codes and roster snapshots are lost; brackets already
generated stay untouched)

Sets `SET LOCAL lock_timeout = '5s'`: if another session holds a lock on
the table, the script aborts after 5 s instead of queueing the site behind it.
Re-run it in a quiet moment. This also applies to `rollback_019.sql`.
The timeout aborts the transaction; idempotency is unchanged. Undo timeout
edits only in these files, never by executing the destructive rollback script.

### 020_add_playoff_phase.sql

Adds the optional playoff phase of a LAN tournament (PRD F-10).

On `lan_tournaments`:

- **`playoff_game_format`, `playoff_elimination_mode`,
  `playoff_release_mode TEXT NULL`** and **`playoff_group_count`,
  `playoff_qualifiers_per_group`, `playoff_qualifier_count INTEGER NULL`** --
  the playoff configuration, all NULL or one complete shape, enforced by
  `ck_lan_tournaments_playoff_config` (round robin groups into a bracket, or
  highscore into FFA lobbies)
- **`playoff_auto_release_suspended BOOLEAN NOT NULL DEFAULT FALSE`**,
  **`playoff_released_at TIMESTAMP NULL`**, **`playoff_released_by UUID NULL`**
  (FK `fk_lan_tournaments_playoff_released_by` to `users.id`) -- release state
- **`leaderboard_closed_at TIMESTAMP NULL`** -- backs "close qualification"

On `lan_tournament_matches`:

- **`phase SMALLINT NOT NULL DEFAULT 1`** -- 1 main phase, 2 playoffs
  (`ck_lan_tournament_matches_phase`); existing rows become phase 1.
  `ix_lan_tournament_matches_tournament_phase` (`tournament_id, phase`)
- **`seeding_target VARCHAR(40) NULL`** -- the seeding draft that generated the
  match (`initial`, `playoff`, `ffa:<pool>:<round>`; the losers round a
  winners round comes with carries the winners target); NULL for matches made
  without a draft. `ix_lan_tournament_matches_tournament_seeding_target`
  (`tournament_id, seeding_target`) backs the delete-by-target of a
  regeneration

New table **`lan_tournament_qualification_decisions`**: an orga's ordering of a
tie, one row per `(tournament_id, scope)`
(`uq_lan_tournament_qualification_decisions_scope`). `ordered_contestant_ids`
is a JSON array string; `ck_lan_tournament_qualification_decisions_reason`
rejects a reason that is empty after trimming spaces, tabs and line breaks.
FKs `fk_lan_tournament_qualification_decisions_tournament_id` and
`..._decided_by`. No CASCADE behaviors (BYCEPS convention).

Every added constraint is guarded by a `pg_constraint` lookup, so a re-run is
a no-op.

Note: the branch `prd/f12-substitutes` also uses 020
(`020_add_team_membership_history.sql`). This collision is a deliberate,
accepted decision (policy: next number actually up) -- whichever of the two
branches is merged second must renumber its migration, its rollback and its
README entry before merging.

**Rollback:** `rollback_020.sql` (drops the decisions table, the seeding target
index and column, the phase index, check and column, then the playoff
check, FK and ten columns -- irreversible: all decisions, playoff configuration and release state are lost, and once
playoffs ran the phase-2 matches lose their marker and become
indistinguishable from phase-1 matches; do not run it after playoffs were
generated)

Sets `SET LOCAL lock_timeout = '5s'`: if another session holds a lock on
the table, the script aborts after 5 s instead of queueing the site behind it.
Re-run it in a quiet moment. This also applies to `rollback_020.sql`.
The timeout aborts the transaction; idempotency is unchanged. Undo timeout
edits only in these files, never by executing the destructive rollback script.

### 021_add_match_ready_columns.sql

F-04 match-ready system: per-side readiness claims on `lan_tournament_matches`:

1. **`occupied_since TIMESTAMP NULL`** — set when both sides of a match are fixed (backfilled for existing fully-occupied matches from contestant creation timestamps)
2. **`ready_at_a` / `ready_at_b TIMESTAMP NULL`** — per-side readiness claim timestamps
3. **`ready_by_a` / `ready_by_b UUID NULL`** — FK to `users.id`; who claimed readiness per side
4. **`both_ready_notified_at TIMESTAMP NULL`** — ready-email marker; cleared on revocation so emails stay suppressed until readiness is newly claimed

Zero CASCADE behaviors (BYCEPS convention). Idempotent (`IF NOT EXISTS`), transaction-wrapped.
There is no revocation record: un-ready clears the side's claim, and the
`match-ready-revoked` audit entry is the only trace.

**Rollback:** `rollback_021.sql` (drops the added columns)

**Staging note:** 021 was edited in place (readiness never reached production,
so no new migration number). A database that already ran the earlier 021 keeps
five extra nullable columns (`ready_revoked_at/by/reason/side/role`). They are
harmless: nothing reads or writes them, and `rollback_021.sql` no longer drops
them. Optional cleanup, a **human action** that nobody runs without approval
(never run by agents or scripts):

```sql
ALTER TABLE lan_tournament_matches DROP COLUMN IF EXISTS ready_revoked_at, DROP COLUMN IF EXISTS ready_revoked_by, DROP COLUMN IF EXISTS ready_revoked_reason, DROP COLUMN IF EXISTS ready_revoked_side, DROP COLUMN IF EXISTS ready_revoked_role;
```


### 022_add_match_readiness_integrity.sql

Additive repair after **021**: BIGINT pairing generation/readiness revision,
nullable retained-pair pointer and two invitation holds; retained opponent
snapshots and recipient invitation work. SQL types, named checks, uniqueness,
defaults and indexes match the ORM. UUID IDs have application uuid7 defaults,
not server defaults; historical inserts use `gen_random_uuid()`.

**Human approval is required before deployment, activation or rollback.** Local
verification is not server authorization. Recheck the highest migration number
in the integration base and sibling branches before merging:

```bash
ls byceps/services/lan_tournament/migrations/[0-9][0-9][0-9]_*.sql | sort
```

022 was free on this snapshot. If occupied, reconcile/renumber apply, rollback,
README and tests together. Never repurpose 021 or orga `rollback_014.sql`.
Prerequisites: base schema through 020 and 021, PostgreSQL 13+, reviewed backup
and maintenance window. Do not blindly replay the old chain (003 is not
idempotent). Both 022 scripts use `BEGIN`/`COMMIT` and
`SET LOCAL lock_timeout = '5s'`; lock contention aborts atomically. Re-run in a
quiet window. Catalog guards are relation-scoped; repeat apply/rollback is safe.

**Backfill/activation contract:** readiness was never deployed. 022 does not
invent or modify Ready actors, timestamps, revocations or the legacy inert
`both_ready_notified_at`. Exactly two distinct active, tournament-owned real
contestants are ordered by `(created_at, id)`. Placeholder slots and FFA are
excluded; phase 2 uses `playoff_game_format`, not the main format. Known current
pair timing is the later of `occupied_since` and current side insertion times;
if original occupancy is unknown, pair start remains NULL. Original occupancy
is untouched. Existing unconfirmed ONGOING audiences (all active team members,
not only captains) get `delivery_unknown`, attempts 0 and no acceptance/lease
facts. Reapplication preserves recorded acceptance and other known work facts.
Pre-start rows get pair snapshots but **no invitations until actual start**:
the repaired start/reconciliation path creates pending work then. Unknown
historical delivery needs explicit human resend judgment, never automatic bulk
resend. Both-ready does not trigger a second email.

After human approval, stop web/workers, apply SQL before activating repaired
code, verify parity/backfill and deploy the matching repaired application:

```bash
docker compose stop web worker
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U byceps byceps < \
  byceps/services/lan_tournament/migrations/022_add_match_readiness_integrity.sql
docker compose start web worker
```

Native equivalent (after backup/approval, with matching code installed):

```bash
systemctl stop byceps-web byceps-worker
psql -v ON_ERROR_STOP=1 -U byceps -h localhost byceps -f \
  /opt/byceps/byceps/services/lan_tournament/migrations/022_add_match_readiness_integrity.sql
systemctl start byceps-web byceps-worker
```

**Rollback is data-losing:** permanently removes new pairing/work history,
generation/revision and hold facts. Back up first and obtain separate human
approval; stop repaired code and install compatible pre-repair code before
restarting. All 021 columns/constraints, orga 014 and audit FK hardening survive.
No retained pairing/work FK points back to deletable live match, tournament or
contestant rows. Only invitation recipient references `users`; the live match
pointer references retained pairing. Application explicitly closes/suppresses
facts before deletion/regeneration; there is no database cascade.

```bash
docker compose stop web worker
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U byceps byceps < \
  byceps/services/lan_tournament/migrations/rollback_022.sql
# Install compatible pre-repair application before restarting web/worker.
docker compose start web worker
```

Native rollback while services are stopped:

```bash
psql -v ON_ERROR_STOP=1 -U byceps -h localhost byceps -f \
  /opt/byceps/byceps/services/lan_tournament/migrations/rollback_022.sql
```

Actual isolated PostgreSQL checks (serialize with the integration lock):

```bash
flock /tmp/opencode/f04-execution/integration.lock bash -c 'source /tmp/opencode/f04-execution/verification-preflight.env; "$PY" -m pytest tests/integration/services/lan_tournament/test_migration_match_readiness.py -q -o addopts="" -p no:cacheprovider'
```

### 023_add_dashboard_operational_timing.sql

Persistent facts of the cross-tournament orga dashboard (PRD F-03).

On `lan_tournaments`:

- **`operational_clock_elapsed_us BIGINT NOT NULL DEFAULT 0`** -- microseconds
  of active time; `ck_lan_tournaments_operational_clock_elapsed_us` requires
  `>= 0`
- **`operational_clock_running_since` / `operational_clock_activated_at
  TIMESTAMP NULL`** -- naive timestamps, like every timestamp of this module

On `lan_tournament_matches`: **`last_changed_at TIMESTAMP NULL`**, the
domain-only last-change time of a match.

Four new tables. Their tournament, match, party and actor IDs are snapshots
**without a foreign key**, so the history survives the deletion or
regeneration of the live rows:

1. **`lan_tournament_match_due_episodes`** -- one row per uninterrupted period
   of due demand. `opened_clock_us` / `closed_clock_us` are `BIGINT` (checked
   `>= 0`), `closed_at` and `closed_clock_us` are both NULL or both set
   (`ck_lan_tournament_due_episodes_close_pair`), and the partial unique index
   `uq_lan_tournament_due_episodes_open_match` (`match_id WHERE closed_at IS
   NULL`) allows one open episode per match. History is retained without
   limit, so two more partial indexes keep the hot reads off a sequential
   scan:
   `ix_lan_tournament_due_episodes_open_tournament` (`tournament_id WHERE
   closed_at IS NULL`) serves `list_open_due_episodes`, which every result
   write calls while it holds the tournament row lock, and
   `ix_lan_tournament_due_episodes_closed_match` (`match_id WHERE closed_at IS
   NOT NULL`) serves the per-fixture prior-episode facts of every dashboard
   snapshot and poll. `rollback_023.sql` needs no index statement: dropping
   the table drops its indexes
2. **`lan_tournament_match_escalation_acks`** -- one row per acknowledged
   episode revision (`uq_lan_tournament_escalation_ack_episode_revision`,
   `clock_us BIGINT`). `fk_lan_tournament_escalation_acks_episode_id` is the
   only foreign key of the four tables, without `ON DELETE`
3. **`lan_tournament_match_dashboard_annotations`** -- the shared pin of a
   live match, keyed by the match ID (`pin_pair` check: `pinned_at` and
   `pinned_by` are both NULL or both set)
4. **`lan_tournament_dashboard_party_thresholds`** -- the Wartung override of
   the traffic thresholds of one party, keyed by the party ID. Whole minutes:
   `1 <= yellow_minutes < red_minutes <= 1440`, each a named check. No row
   means the deployment default

Every microsecond column is `BIGINT`: an `INTEGER` holds only 35 min 47 s,
below the 45 min default red threshold.

**Unknown history stays unknown.** 023 backfills nothing: existing
tournaments keep an elapsed time of 0 and NULL timestamps, existing matches
keep a NULL `last_changed_at`. The dashboard shows such timing as unavailable.

**Cleanup contract.** There is no database cascade. The application removes
the pin row together with its match; due episodes, acknowledgements and party
thresholds are retained. No index is added for unconfirmed match frontiers or
active team memberships: no query needs one yet.

**Human approval is required before deployment, activation or rollback.**
Local verification is not server authorization. Recheck the highest migration
number in the integration base and sibling branches first; if 023 is occupied,
renumber apply, rollback, README and test together:

```bash
ls byceps/services/lan_tournament/migrations/[0-9][0-9][0-9]_*.sql | sort
```

Prerequisites: schema through 022, PostgreSQL 13+, reviewed backup and a
maintenance window. Both scripts use `BEGIN`/`COMMIT` and
`SET LOCAL lock_timeout = '5s'`: lock contention aborts atomically, re-run in a
quiet window. They are idempotent and every guard is scoped to its relation.
Apply before the tournaments start: 023 does not reconstruct the operational
clock of a tournament that already ran.

```bash
docker compose stop web worker
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U byceps byceps < \
  byceps/services/lan_tournament/migrations/023_add_dashboard_operational_timing.sql
docker compose start web worker
```

Native equivalent (after backup/approval, with matching code installed):

```bash
systemctl stop byceps-web byceps-worker
psql -v ON_ERROR_STOP=1 -U byceps -h localhost byceps -f \
  /opt/byceps/byceps/services/lan_tournament/migrations/023_add_dashboard_operational_timing.sql
systemctl start byceps-web byceps-worker
```

**Rollback is data-losing:** it drops the four tables with every episode,
acknowledgement, pin and threshold override, then the three clock columns, the
elapsed check and `last_changed_at`. Nothing else is touched. Back up first,
obtain separate human approval, stop the dashboard code and install code that
predates 023 before restarting.

```bash
docker compose stop web worker
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U byceps byceps < \
  byceps/services/lan_tournament/migrations/rollback_023.sql
# Install code that predates 023 before restarting web/worker.
docker compose start web worker
```

Native rollback while services are stopped:

```bash
psql -v ON_ERROR_STOP=1 -U byceps -h localhost byceps -f \
  /opt/byceps/byceps/services/lan_tournament/migrations/rollback_023.sql
```

Isolated PostgreSQL checks (the test creates and drops its own schemas on the
configured `byceps_test*` database; it refuses any other database name):

```bash
POSTGRES_DB=byceps_test pytest tests/integration/services/lan_tournament/test_migration_dashboard_timing.py -q -o addopts="" -p no:cacheprovider
```

#### 023: Pre-start requirement

The operational clock of a tournament starts when it goes from
`REGISTRATION_CLOSED` to `ONGOING` while 023 and the matching code are live,
runs only while it is `ONGOING`, and freezes on a pause. A tournament that
started earlier has no clock and never gets one, so a supported target party is
one **without a tournament that has already started** when 023 and the code go
live. Check it before applying, and record the database, the party and the time
of the check in the human report:

```sql
SELECT id, name, tournament_status
FROM lan_tournaments
WHERE party_id = '<party_id>'
  AND tournament_status IN ('ONGOING', 'PAUSED', 'COMPLETED', 'CANCELLED');
```

No row means the party is a supported target. A `CANCELLED` row alone does not
say whether that tournament ever ran: look at its matches. Any tournament found
makes the party an unsupported target for the full feature. Its tournaments
keep running, but their timing is unavailable (below). Support for such a party
needs a new authorization. A tournament created or moved straight into a
started status through service input has no clock history either.

#### 023: Unknown history

023 backfills nothing and the application invents nothing:

- A tournament with `operational_clock_activated_at IS NULL` has an unknown
  clock. The dashboard shows its timing as unavailable (tier unknown), refuses
  every acknowledgement for it (reason: clock unknown) and keeps its due matches
  in the conflict detection. Pause and resume never create time for it.
- A match with `last_changed_at IS NULL` has an unknown last change. It shows as
  unavailable until an actual domain change of that match (score, confirmation,
  contestants, readiness, walkover, removal) stamps it. A repeat of the same
  value, comments, pins and acknowledgements never do.
- The audit log and `updated_at` are not a clock authority and are never used
  to reconstruct either.

#### 023: Dashboard configuration

The deployment settings are read by `dashboard_config.py` when a request is
handled, never at import. `test_dashboard_config.py` pins this table.

| Key | Default | Allowed | Meaning |
|-----|---------|---------|---------|
| `LAN_TOURNAMENT_DASHBOARD_YELLOW_MINUTES` | `15` | whole minutes, 1 to 1440, below red | yellow from this alert interval |
| `LAN_TOURNAMENT_DASHBOARD_RED_MINUTES` | `45` | whole minutes, 1 to 1440, above yellow | red from this alert interval |
| `LAN_TOURNAMENT_DASHBOARD_POLL_SECONDS` | `30` | whole seconds, 5 to 300 | refresh interval the page and the poll announce |
| `LAN_TOURNAMENT_DASHBOARD_PAGE_SIZE` | `50` | whole number, 1 to 100 | rows per page; never a query parameter |

Per key the order is: app config key of that name, else the environment
variable of that name, else the default. The core assembles no such key into the
app config, so on a deployment the environment variable of the web processes
(admin and every site app) is the way to set it; restart them after a change.
The environment value is parsed as JSON: `20` is a number, while `20.0`, `true`,
`"20"`, `20s` and an empty value are refused. A value that is set but invalid
is an error, never a fallback to the next source. The checks run in a fixed
order (yellow, red, their order, poll, page size) with the error codes
`invalid_dashboard_yellow_minutes`, `invalid_dashboard_red_minutes`,
`invalid_dashboard_threshold_order`, `invalid_dashboard_poll_seconds` and
`invalid_dashboard_page_size`. A broken value is an operator error: the
dashboard routes answer `500`, while the Wartung card shows the error instead of
its form and the admin back link is hidden. The site back link reads no
settings and is unaffected.

A party override in the Wartung tab (permission `lan_tournament.maintain`)
replaces only yellow and red for that party, on both dashboards. Poll interval
and page size always come from the deployment. No row means the deployment
default. Changes are logged to the application log, not to the audit log.

#### 023: History retention

- Episodes, acknowledgements and party thresholds keep their tournament, match,
  party and actor IDs as snapshots, without a foreign key. They survive the
  deletion of a tournament, the regeneration of a bracket and an un-release;
  open episodes of removed matches are closed at the operation time and clock.
  The pin row of a match is removed with the match.
- They do not depend on the audit log. `purge_lan_tournament_log_entries` only
  deletes log entries. The audit entries `match-pinned`, `match-unpinned` and
  `match-acknowledged` can be purged and hold no comment: the acknowledgement
  row is the durable record of actor, time and comment.
- No application path or CLI removes episodes or acknowledgements. They stay
  until a human removes them, or until `rollback_023.sql` drops the tables. A
  party override is removed by a reset in the Wartung tab.

#### 023: Manual release responsibilities

The agent work ends at reviewed code, tests and documentation. These steps stay
with the human:

1. Approving and applying `023_add_dashboard_operational_timing.sql` (and any
   `rollback_023.sql`) on the target database, after a backup and with the
   pre-start check above.
2. Compiling the German message catalogue (`.mo`) on the server. The repository
   carries only `.po` files, and a new msgid stays English until the catalogue
   is compiled.
3. Staging QA with real data, including 360 px and the totalverplant theme, and
   the visual sign-off against the accepted design.
4. Git publication: commit, push and merge.

### 024_add_tournament_category.sql

Adds `lan_tournaments.category TEXT NOT NULL DEFAULT 'MAIN'`, guarded by
`ck_lan_tournaments_category` (`MAIN`, `FUN`, `STAGE`, `USER_ORGANIZED`).
On first application, existing request-derived tournaments are backfilled to
`USER_ORGANIZED`; all others receive `MAIN`. Repeated application preserves
category selections, including request-derived tournaments promoted to `MAIN`.
Request provenance and category are independent. Positions remain unchanged.
Apply after 023; `create_all()` does not migrate existing tables.

**Rollback:** `rollback_024.sql` removes only the category constraint and
column. Category selections are lost; tournaments, requests, provenance,
positions, images, creation tokens and orga assignments are preserved.

## Pre-Application Checklist

Before applying any migration, complete these steps:

### 1. Create Database Backup

**Docker deployment:**
```bash
docker compose exec -T db pg_dump -U byceps byceps | gzip > backup-$(date +%Y%m%d-%H%M%S).sql.gz
```

**Native deployment:**
```bash
pg_dump -U byceps -h localhost byceps | gzip > backup-$(date +%Y%m%d-%H%M%S).sql.gz
```

Store backups in a safe location with sufficient retention period.

### 2. Verify Prerequisites

- PostgreSQL version 13 or higher
- Sufficient disk space (check with `df -h`)
- Database user has CREATE TABLE permissions
- No active user sessions during migration (coordinate maintenance window)

### 3. Stop Application Services

**Docker:**
```bash
docker compose stop web worker
```

**Native:**
```bash
systemctl stop byceps-web byceps-worker
```

Leave database running - only stop application services.

## Application Instructions

### Docker Deployment

Apply migration:
```bash
docker compose exec -T db psql -U byceps byceps < \
  byceps/services/lan_tournament/migrations/001_create_lan_tournament_schema.sql
```

Expected output:
```
BEGIN
CREATE TABLE
CREATE INDEX
...
COMMIT
```

### Native Deployment

Apply migration:
```bash
psql -U byceps -h localhost byceps < \
  /opt/byceps/byceps/services/lan_tournament/migrations/001_create_lan_tournament_schema.sql
```

### Post-Application Steps

1. Verify migration success (see Verification Queries below)
2. Restart application services
3. Monitor application logs for errors
4. Test tournament functionality in staging/development first

## Verification Queries

After applying migration, verify schema was created correctly:

### Verify All Tables Exist

```sql
SELECT table_name
FROM information_schema.tables
WHERE table_name LIKE 'lan_tournament%'
ORDER BY table_name;
```

Expected output: 6 tables
- `lan_tournament_match_comments`
- `lan_tournament_match_contestants`
- `lan_tournament_matches`
- `lan_tournament_participants`
- `lan_tournament_teams`
- `lan_tournaments`

### Verify Indexes

```sql
SELECT indexname
FROM pg_indexes
WHERE tablename LIKE 'lan_tournament%'
ORDER BY indexname;
```

Expected output: 7 indexes
- `ix_lan_tournament_match_comments_match_id`
- `ix_lan_tournament_match_contestants_match_id`
- `ix_lan_tournament_matches_tournament_id`
- `ix_lan_tournament_participants_tournament_id`
- `ix_lan_tournament_participants_user_id`
- `ix_lan_tournament_teams_tournament_id`
- `ix_lan_tournaments_party_id`

### Verify Constraints

```sql
SELECT conname, contype
FROM pg_constraint
WHERE conname LIKE '%lan_tournament%'
ORDER BY conname;
```

Expected constraint types:
- `c` = CHECK constraints (11 validation rules)
- `f` = Foreign key constraints (9 relationships)
- `p` = Primary key constraints (6 tables)
- `u` = UNIQUE constraints (3 uniqueness rules)

### Verify No CASCADE Behaviors

```sql
SELECT
    tc.table_name,
    kcu.column_name,
    ccu.table_name AS foreign_table_name,
    rc.delete_rule
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
  ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage AS ccu
  ON ccu.constraint_name = tc.constraint_name
JOIN information_schema.referential_constraints AS rc
  ON rc.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_name LIKE 'lan_tournament%'
ORDER BY tc.table_name, kcu.column_name;
```

**Expected:** All `delete_rule` values should be `NO ACTION` (never `CASCADE` or `SET NULL`).

## Rollback Procedure

If migration causes issues, you can rollback:

### Option 1: Restore from Backup (Recommended)

```bash
# Docker
gunzip < backup-YYYYMMDD-HHMMSS.sql.gz | docker compose exec -T db psql -U byceps byceps

# Native
gunzip < backup-YYYYMMDD-HHMMSS.sql.gz | psql -U byceps -h localhost byceps
```

### Option 2: Use Rollback Script

⚠️ **WARNING: This deletes all tournament data!**

```bash
# Docker
docker compose exec -T db psql -U byceps byceps < \
  byceps/services/lan_tournament/migrations/rollback_001.sql

# Native
psql -U byceps -h localhost byceps < \
  /opt/byceps/byceps/services/lan_tournament/migrations/rollback_001.sql
```

After rollback:
1. Verify tables were dropped
2. Investigate migration failure
3. Fix issues before re-applying

## Troubleshooting

### ERROR: relation already exists

**Cause:** Migration was partially applied or tables exist from previous attempt.

**Solution:** Migration is idempotent - safe to run again. The `IF NOT EXISTS` clauses prevent errors.

### ERROR: duplicate key violates unique constraint

**Cause:** Attempting to add UNIQUE constraint when duplicate data exists.

**Solution for 002 (if applying separately):**
```sql
-- Find duplicates before adding constraint
SELECT tournament_id, name, COUNT(*)
FROM lan_tournament_teams
GROUP BY tournament_id, name
HAVING COUNT(*) > 1;
```

Clean up duplicates before applying constraint.

### ERROR: foreign key violation

**Cause:** Referenced table or row doesn't exist.

**Solution:** Ensure parent tables exist:
- `parties` table must exist before creating tournaments
- `users` table must exist before creating teams/participants

### ERROR: permission denied

**Cause:** Database user lacks CREATE TABLE permission.

**Solution:**
```sql
GRANT CREATE ON SCHEMA public TO byceps;
```

### ERROR: disk full

**Cause:** Insufficient disk space for tables/indexes.

**Solution:**
- Free up disk space
- Check available space: `df -h`
- Consider moving to larger volume

### Transaction Aborted

**Cause:** Any error in migration causes full rollback due to `BEGIN;...COMMIT;` wrapper.

**Solution:**
- Check error message for specific issue
- Fix underlying problem
- Re-run migration (idempotent design handles this)

## Migration Testing

**Always test migrations in development environment first:**

1. Create test database from production backup
2. Apply migration to test database
3. Verify schema with queries above
4. Test application functionality
5. Measure migration time for scheduling production window
6. Document any issues encountered

## References

- **Deployment Guide:** `/workspace/ai_docs/deployment-guide.md` (lines 414-421)
- **Service Implementation:** `/workspace/byceps/services/lan_tournament/`
- **CASCADE Tests:** `/workspace/tests/unit/services/lan_tournament/test_tournament_deletion.py`
- **Database Models:** `/workspace/byceps/services/lan_tournament/dbmodels/`

## Support

For migration issues:
1. Check application logs: `docker compose logs -f` or `journalctl -u byceps-web`
2. Verify database connectivity
3. Review this troubleshooting section
4. Consult deployment guide for general procedures
