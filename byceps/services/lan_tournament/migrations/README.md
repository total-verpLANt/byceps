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

- `tournament_service.py::delete_tournament()` - Deletes the tournament and its dependencies (submissions, comments, contestants, matches, winner references, participants, teams). Log entries are explicitly NOT among them (see 013 below) -- they are orphaned, not deleted, and a `tournament-deleted` entry is written recording the deletion itself.
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
2. **`occurred_at TIMESTAMPTZ NOT NULL`** — when the event happened, indexed via `ix_lan_tournament_log_entries_occurred_at` (the retention purge and its `--dry-run` count filter on this column alone)
3. **`event_type TEXT NOT NULL`** — event discriminator string
4. **`tournament_id UUID NOT NULL`** — FK to `lan_tournaments.id`, indexed via `ix_lan_tournament_log_entries_tournament_id`
5. **`initiator_id UUID NULL`** — nullable FK to `users.id` (system-triggered entries have no initiator)
6. **`data JSONB NOT NULL DEFAULT '{}'::jsonb`** — structured payload

Shape mirrors the stock tourney log table (`byceps/services/tourney/log/dbmodels.py`). Zero CASCADE behaviors (BYCEPS convention).

**Rollback:** `rollback_012.sql` (drops both indexes, then table)

Retention: old log entries can be purged with the CLI command `byceps purge-lan-tournament-log-entries --older-than-days N [--dry-run]` (default 365 days; see `byceps/cli/commands/purge_lan_tournament_log_entries.py`), which hard-deletes rows older than the given number of days. Use `--dry-run` to report how many entries would be deleted without deleting them. The command's CLI module lives in BYCEPS core (`byceps/cli/commands/`, registered in `byceps/cli/cli.py`) by explicit, granted exception to the module's core-is-read-only rule.

### 013_drop_log_entry_tournament_fk.sql

Drops `fk_lan_tournament_log_entries_tournament_id`. The `tournament_id` column, its `NOT NULL` constraint, and `ix_lan_tournament_log_entries_tournament_id` are all kept -- only the FK goes.

**Why:** that FK forced every log entry for a tournament to be deleted before the tournament row itself could go, and `tournament_service.py::delete_tournament()` used to do exactly that as step 1 of its own cascade -- meaning the same `lan_tournament.administrate` role the log exists to hold accountable could also erase it, just by deleting the tournament (workspace-ytqz). As of this migration, `delete_tournament()` no longer touches log entries. Deleting a tournament now **orphans** its entries (`tournament_id` pointing at a row that no longer exists) instead of removing them, and additionally writes one further `tournament-deleted` entry with denormalised tournament context (name, party, game, status) so it stays meaningful with no tournament row left to join to.

The retention purge (see 012 above) is unaffected and is now the *only* thing that ever removes log entries: it filters on `occurred_at` alone, with no join to `lan_tournaments`, so orphaned entries age out exactly like any other entry.

**Rollback:** `rollback_013.sql` -- re-adds the FK, but read the warning at the top of that file first: `ADD CONSTRAINT` validates every existing row and will fail once any tournament has been deleted since 013 was applied, because that produces exactly the orphaned rows described above. The rollback does not delete or re-point those rows to force it through; that decision is left to the operator (see the file for options), because auto-deleting them would silently recreate the defect 013 fixes.

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

Note: BYCEPS resolves a session's permissions at login. Anyone already signed
in must log out and back in before the grant takes effect — a re-run of the
grant will not help them.

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
