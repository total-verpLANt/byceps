# Participant chair information

This extension adds organizer reports and participant reminders to BYCEPS Core's
per-ticket chair selection. It requires Core through commit `0159fe89f149`
(including `8fc0d8a91`, `ad9f93483`, and `a8a18b561bf7`). Core's endpoint,
chair setter, ticket services, and ticket logs are reused.

## Core data and participant interface

The only answer storage is `tickets.chair_source`:

- `user`: brings their own chair;
- `venue`: needs a chair provided by the location;
- `rental`: reports a rented chair;
- `unknown`: has not specified a source. Core also reads raw SQL `NULL` or
  unrecognized stored values as `unknown`.

Both the standard interface and GV36 offer own/venue chairs on `My tickets`.
The party-local rental switch adds "I expect a rented chair" in GV36 and the
Core rental option in the standard interface. It defaults to OFF. Rental is a
participant statement, with no booking, payment, inventory reservation, or shop
action. When OFF, new rental POSTs are rejected; existing rental values remain
visible and distinctly counted/exported. The standard Core reset to `unknown`
remains available. GV36 has no manual reset option or separate chair form.
New tickets start with `unknown`, restored automatically on participant changes.
Legacy `/chair_optout/` GET links redirect to `My tickets`.

The GV36 dashboard and seating templates link to the first unanswered ticket.
Multiple pending tickets are counted on the dashboard. Reminders cover tickets
with a current participant that are non-revoked and unchecked-in, and hints are
hidden when ticket management is disabled. The participant and the ticket's
user manager may edit.
The owner is the default user manager only until management is delegated;
seat management alone does not grant permission. Reminders also follow this
current edit permission. Own-profile information is
rendered in the site override using the tickets already provided by Core.

## Transactions and ticket lifecycle

A request hook guards only Core's `ticketing.set_chair_source` POST endpoint.
It locks and refreshes the ticket before checking current party, participant or
user-manager permission, revocation and check-in state, and the rental setting.
Core performs the chair update and ticket-log write and commits once, using the
actual initiating user from `g.user.as_user()`.
Request/session teardown rolls back failed requests.

A feature-local mapper listener resets the source in the participant-change
transaction. It compares the database-current participant under a row lock with
the intended participant, preserving answers on duplicate appointments. It
forces a reset even if a stale ORM instance had already loaded `unknown`.
Seat changes retain answers; changing or withdrawing the participant clears
them, and returning to a previous participant does not restore an old answer.

`application.py` registers the listener idempotently for all application modes,
including CLI and worker. Participant changes must use the normal ORM ticket
services: bulk SQL updates bypass mapper listeners. The HTTP guard applies to
the Core chair route; direct service calls must validate/lock their own access.
Automatic participant-change resets accompany the existing participant log,
while explicit selections use Core's `chair-source-set` ticket event.

## Administration

The party's `More` page links to `Seat management`, then `Participant chair
information`. Reports use `seating.view` for the list, plan and CSV.
The dedicated `Rental chair selection` tab shows the ON/OFF state to readers and a
validated change form to users with `party.update`. The setting is stored through
`party_setting_service` as `chair_rental_selection_enabled` (`true`/`false`);
a missing value means OFF. Each party has its own setting. Turning it OFF retains
all recorded rental answers and their report filters and seating markers.
Activating a previously disabled selection requires a confirmation dialog;
cancelling keeps it OFF without submitting the form.

Source counts partition non-revoked party tickets with a current participant.
`No seat` is an overlapping additional count, not another source category.
Rental filters in both reports and the plan legend are shown when selection is
enabled or rental records exist. Only actual rental answers mark seats, using a
purple outline and diamond. Unknown answers are
never counted as confirmed venue-chair demand.

The plan loads a compact ticket/source map instead of the participant report.
Filters dim nonmatching seats without removing geometry. The no-seat filter
is available only in the participant list. Own chairs have a green outline/dot,
venue chairs a blue marker, and unknown/rental sources have distinct markers and text
tooltips. The module-local tooltip script uses DOM text nodes, independently of
Core seating behavior. CSV cells retain formula-injection escaping.

GV36 chair choices use Core POST requests followed by a background page GET.
The GET identifies the affected ticket by ID and verifies the current source;
session-wide flash messages are consumed but are not evidence of this ticket's
save. A matching source gets a ticket-local, localized inline confirmation,
including when the flash was already consumed or belongs to another ticket.
There is no navigation or scroll jump. Selecting the displayed source first
verifies the server state with a GET. Only a verified match closes the menu
without a POST or ticket-log entry; a stale display triggers a regular save.
A failed precheck shows an inline error and permits retry. If a POST succeeds
but its refresh fails or contains invalid ticket data, the old display is marked
uncertain and the error distinguishes the successful request from the unverified
answer. The next explicit choice can be saved again. A conflicting refreshed
source is displayed with a retry message instead of a false success. Open ticket
dropdowns remain above other ticket cards and the footer in both themes.

Public seat links use the party's `primary_party_site_id`, or the unique current
site if no primary site is configured, and use HTTPS like Core cross-site links.
The plan uses that site's seating stylesheet when available. Without a primary
site it uses the unique party-site stylesheet; ambiguous choices are not guessed.
Stylesheet paths are validated and Core seat dimensions are the fallback.

## Restart local test chair data

Fresh installations use only Core's nullable text column and require no
extension migration. Legacy answers are intentionally not transferred. For an
existing local test runtime, inspect `tickets.chair_source` and any remaining
`party_ticket_chair_optouts` table (including columns and dependencies), back up
the database under `runtime/backups/`, and stop application and worker processes
before explicitly running this one-time reset:

```sh
psql -X -v ON_ERROR_STOP=1 "$DATABASE_URL" \
  -f byceps/services/chair_optout/scripts/reset_test_chair_data.sql
```

This transaction resets only `tickets.chair_source` to `unknown` and removes the
old answer table if present, after verifying its expected columns. Unexpected
columns or external dependencies abort the complete operation; no `CASCADE` is
used. Core's column, ticket identities/assignments/seats, parties, users,
tournaments, and historical ticket logs are retained. Re-running it deliberately
clears any answers entered since the preceding reset. It is not an automatic
installation step. Restore the backup to undo a data reset.

## Validation

Integration tests drop tables. Use a disposable PostgreSQL database and Redis,
never the shared runtime. Configure `POSTGRES_HOST`, `POSTGRES_PORT`,
`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `REDIS_HOST`, and `REDIS_PORT`.
Check the effective app targets before running integration tests: inherited
`SQLALCHEMY_DATABASE_URI` and `REDIS_URL` can override those explicit settings.
The following commands remove these overrides; the host/port variables must
still point to explicitly verified disposable resources.

```sh
uv sync --frozen --group test
uv run --no-sync pytest tests/unit/services/chair_optout \
  tests/unit/blueprints/chair_optout tests/unit/test_browser_chair_fixture.py \
  tests/unit/services/lan_tournament/test_more_items_patch.py
env -u SQLALCHEMY_DATABASE_URI -u REDIS_URL uv run --no-sync pytest \
  tests/integration/services/chair_optout \
  tests/integration/blueprints/admin/chair_optout \
  tests/integration/blueprints/site/chair_optout \
  tests/integration/blueprints/site/dashboard \
  tests/integration/blueprints/site/user_profile \
  tests/integration/blueprints/admin/more
env -u SQLALCHEMY_DATABASE_URI -u REDIS_URL uv run --no-sync \
  coverage run --source=byceps -m pytest tests
```

The tests exercise sources, eligibility, rental OFF/ON, user-manager permissions,
the targeted data reset/rollback, locking and stale instances, concurrent
answers/appointments, Core log writes, theme reminders,
filters, CSV, and fresh CLI/worker registration. Run browser regressions from
the repository root:

```sh
docker run --rm --ipc=host -v "$PWD:/repo:ro" -w /repo \
  mcr.microsoft.com/playwright:v1.56.1-noble sh -c \
  'npm install --prefix /tmp --ignore-scripts --no-package-lock playwright@1.56.1 && NODE_PATH=/tmp/node_modules node tests/browser/chair_tooltips.cjs'
```

Ticket interaction regressions render the actual GV36 templates without a
database and simulate only Core's HTTP response. They cover light/dark desktop
and mobile layouts, dropdown hit targets over cards/footer, inline confirmation
without navigation, compatibility with older markup, repeated selections,
persistence after reload, rental OFF/ON and retained rental answers after OFF,
the initial unknown DOM status, failed-save retry, consumed/unrelated flashes,
stale answers after failed refreshes or another editor, verified no-ops without
writes/logs, failed precheck retry, conflicting/invalid refreshes, and correct
synchronization of multiple tickets. Integration tests also
render the standard ticket page for both switch states and all chair-edit roles.
Store the generated fixture in the local runtime directory:

```sh
RUNTIME=/home/dennis/projekte/Byceps/runtime/chair-review-fixes-20261002
mkdir -p "$RUNTIME"
FIXTURE="$RUNTIME/ticket-chair-browser-fixture.json"
uv run --no-sync python tests/browser/render_ticket_chair_fixture.py > "$FIXTURE"
docker run --rm --ipc=host -v "$PWD:/repo:ro" \
  -v "$RUNTIME:/fixtures" -w /repo \
  mcr.microsoft.com/playwright:v1.56.1-noble sh -c \
  'npm install --prefix /tmp --ignore-scripts --no-package-lock playwright@1.56.1 && NODE_PATH=/tmp/node_modules node tests/browser/ticket_chair_interactions.cjs /fixtures/ticket-chair-browser-fixture.json'
```

German translations remain in `byceps/translations/de/LC_MESSAGES/messages.po`.
The Docker build compiles the catalogue before installing the project into the
image. Local source execution may require `just babel-compile` for updated UI
translations. To validate without replacing an existing local catalogue, use
`uv run --no-sync pybabel compile -i byceps/translations/de/LC_MESSAGES/messages.po
-o "$RUNTIME/messages.mo" -l de`. Never commit generated `messages.mo`, and
preserve any pre-existing local catalogue changes. Tests must not assume newly
added strings are already translated in the committed catalogue.

Admin browser acceptance tests use the real admin application, stored settings,
ticket sources and authenticated writer/reader sessions. With the isolated
PostgreSQL/Redis environment variables above, create a separate database named
`byceps_chair_followup_browser` and run:

```sh
POSTGRES_DB=byceps_chair_followup_browser uv run --no-sync python \
  -m tests.browser.serve_admin_chair_fixture /path/to/runtime/admin-chair-fixture.json
# In a second terminal with Playwright/Chromium installed:
node tests/browser/admin_chair_interactions.cjs /path/to/runtime/admin-chair-fixture.json
```

The fixture checks the effective database and Redis URLs against its explicit
configuration before initializing resource clients, recreating tables or writing
test data. Conflicting overrides fail without exposing credentials. Its app and
bootstrap use the same database; Redis uses database 1 on the explicit server.
The fixture server recreates only that dedicated browser database and listens
on `127.0.0.1:58080`. The browser test covers all four visibility states, tab and
filter navigation, cancelled/confirmed activation, party-local persistence,
disabling with retained answers, purple diamonds and read-only authorization.
Ticket and admin screenshots are saved beside their fixture JSON in the runtime
directory. If Playwright/Chromium is installed locally, the ticket command can
also be run as `node tests/browser/ticket_chair_interactions.cjs "$FIXTURE"`.
