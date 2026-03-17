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

- `tournament_service.py::delete_tournament()` - Deletes tournament and all dependencies
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
