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

Note: the branch `prd/f04-match-ready` also uses 014 (`014_add_match_ready_columns.sql`). Whichever of the two branches is merged second must renumber its migration, its rollback and its README entry before merging.

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

Note: check the highest migration number in sibling branches before merging;
014 is already shared with `prd/f04-match-ready`, so 015 may need renumbering
along with it, together with its rollback and this entry.

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
