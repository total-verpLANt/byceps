# Chair Opt-out Blueprint

## URLs
- User: GET/POST `/party/<party_id>/chair-optout`
- Admin: GET `/admin/party/<party_id>/chair-optout`
- Admin CSV: GET `/admin/party/<party_id>/chair-optout/export.csv`

## Permissions
- `chair_optout.view_report`
- `chair_optout.export_report`

## Database
- New table: `party_ticket_chair_optouts`
- Create via DDL/SQL or re-run `byceps create-database-tables`.

## Tests
- Install test dependencies: `uv sync --frozen --group test`
- Run tests: `uv run pytest`
