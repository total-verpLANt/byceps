# Chair Opt-out Blueprint

## Purpose
- Users can mark party tickets as "bring own chair".
- Admin report/CSV lists only tickets with brings_own_chair = true.

## Blueprints and templates
- Site blueprint: byceps/services/chair_optout/blueprints/site (name: chair_optout)
- Admin blueprint: byceps/services/chair_optout/blueprints/admin (name: chair_optout_admin)
- Templates:
  - byceps/services/chair_optout/blueprints/site/templates/site/chair_optout/*.html
  - byceps/services/chair_optout/blueprints/admin/templates/admin/chair_optout/*.html
- Registered in:
  - byceps/blueprints/site.py with base path /chair_optout
  - byceps/blueprints/admin.py with base path /chair_optout

## URLs
- User: GET/POST `/party/<party_id>/chair-optout`
- Admin: GET `/admin/party/<party_id>/chair-optout`
- Admin CSV: GET `/admin/party/<party_id>/chair-optout/export.csv`

## Report/CSV columns
- Name | Nickname | Ticketnummer | Sitzplatz-Label

## Seat label resolution
- Seat label is derived live from the current seat assignment.
- Source: ticket.occupied_seat.label (via seat_reservation_service managed tickets).

## Permissions
- `chair_optout.view_report`
- `chair_optout.export_report`

## Database
- New table: `party_ticket_chair_optouts`
- Fields: id (UUID PK), party_id, ticket_id, user_id, brings_own_chair, updated_at
- Unique constraint: (party_id, ticket_id)
- Create via DDL/SQL or re-run `byceps create-database-tables`.

## Tests
- Install test dependencies: `uv sync --frozen --group test`
- Run tests: `uv run pytest tests/unit`
