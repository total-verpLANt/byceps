# Chair Opt-out (Bring Your Own Chair)

## Purpose
- Users can mark individual **party tickets** as “bring own chair”.
- Admin report/CSV lists **only tickets with** `brings_own_chair = true`.

---

## Structure

### Service
- `byceps/services/chair_optout/`
  - service code, DB models, permissions

### Blueprints
- Site blueprint: `byceps/services/chair_optout/blueprints/site` (name: `chair_optout`)
- Admin blueprint: `byceps/services/chair_optout/blueprints/admin` (name: `chair_optout_admin`)

### Templates
- Site templates:
  - `byceps/services/chair_optout/blueprints/site/templates/site/chair_optout/*.html`
- Admin templates:
  - `byceps/services/chair_optout/blueprints/admin/templates/admin/chair_optout/*.html`

### Registration / Integration
- Registered in:
  - `byceps/blueprints/site.py` → module `services.chair_optout.blueprints.site`
  - `byceps/blueprints/admin.py` → module `services.chair_optout.blueprints.admin`
- Entry points are exposed by the routes below; links can be added via templates/navigation as needed.

---

## URLs
- User: `GET/POST /party/<party_id>/chair-optout`
- Admin: `GET /admin/party/<party_id>/chair-optout`
- Admin CSV: `GET /admin/party/<party_id>/chair-optout/export.csv`

---

## Report / CSV columns
- `Name | Nickname | Ticketnummer | Sitzplatz-Label`

---

## Seat label resolution
- Seat label is derived live from the current seat assignment (no seat label is stored in the opt-out table).
- If a ticket has no seat, the seat label is empty/none.

---

## Permissions
- `chair_optout.view`

---

## Database
- New table: `party_ticket_chair_optouts`
- Fields: `id (UUID PK)`, `party_id`, `ticket_id`, `user_id`, `brings_own_chair`, `updated_at`
- Unique constraint: `(party_id, ticket_id)`

Database setup:
`initialize-database` importiert nur Default-Rollen; Custom-Rollen müssen
separat per `import-roles -f` importiert werden.

Fresh install (Docker):
1. `uv run byceps initialize-database`
2. `uv run byceps import-roles -f scripts/data/verplant_roles.toml`
3. `uv run byceps create-superuser`

Upgrade/existing DB:
1. `uv run byceps create-database-tables`
2. `uv run byceps import-roles -f scripts/data/verplant_roles.toml`
3. Hinweis: Rolle anschließend einem Orga-User zuweisen (UI), falls nötig.

---

## Tests
Install test dependencies:
- `uv sync --frozen --group test`

Run tests:
- Unit tests: `uv run pytest tests/unit`
- Full suite: `uv run pytest`
