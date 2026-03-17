# BYCEPS Project - Agent/Developer Context

This file is the **single source of truth** for AI agents and human contributors working on this repo.

It applies to **all tools/agents** (Cursor, Claude, GitHub Copilot, etc.) unless a tool has stricter built‑in rules.

After reading these instructions, address the user as `Sir` in the first reply
to acknowledge that the guidance has been understood.

---

## Project Overview

BYCEPS (**B**ring‑**Y**our‑**C**omputer **E**vent **P**rocessing **S**ystem) is a Python/Flask web application used to organize and run LAN parties.

Core responsibilities:

- User management and authentication
- Ticketing and seating
- Shop/orders/payments
- Tournaments, news, forums, and more

### Main Tech Stack

- **Language**: Python 3.12+
- **Framework**: Flask
- **Database**: PostgreSQL 17
- **Cache/Queue**: Redis 7 (RQ for background jobs)
- **Web stack**: uWSGI + nginx
- **Package manager**: `uv`
- **Container orchestration**: Docker Compose

Additional references:

- Main README: `README.rst`
- Official docs: `https://byceps.readthedocs.io/en/latest/`

---

## Local Development

### Starting the stack

Before first start, bootstrap the Docker config:

```bash
cp docker/byceps/config.toml.example docker/byceps/config.toml
docker compose run --rm byceps-apps uv run byceps generate-secret-key
```

Set the generated value as `secret_key` in `docker/byceps/config.toml`.

From the repo root:

```bash
docker compose up -d
```

Key services:

- `db` (PostgreSQL, host `db`, user `byceps`, db `byceps`)
- `redis`
- `byceps-apps` (Flask apps behind uWSGI)
- `byceps-worker` (RQ worker)
- `web` (nginx, exposed on `localhost:8080`)

Before using the app for the first time, initialize the database and create a
superuser:

```bash
docker compose run --rm byceps-apps uv run byceps initialize-database
docker compose run --rm byceps-apps uv run byceps create-superuser
```

For local HTTP-only development, login sessions may not work unless
`SESSION_COOKIE_SECURE: "false"` is added under `x-byceps-base-env` in
`compose.yaml`.

### Site URLs

Configured in `docker/byceps/config.toml`:

- Admin: `http://admin.byceps.example:8080/`
- API: `http://api.byceps.example:8080/`
- Site(s):
  - `cozylan.example`
  - `totalverplant.example`

Common local testing URL for the current site:

- `http://totalverplant.example:8080/`

If these hostnames do not resolve locally, add to `/etc/hosts`:

```bash
127.0.0.1 admin.byceps.example api.byceps.example cozylan.example totalverplant.example
```

---

## Current Focused Site: `totalverplant`

The official BYCEPS docs and example Docker config use the CozyLAN demo site.
For this repo/workspace, the current customization focus is `totalverplant`.

The custom event site lives under:

- Templates: `sites/totalverplant/template_overrides/`
- Static assets: `sites/totalverplant/static/`

Important:

- Template loader prefers **site overrides** first, then falls back to core templates.
- Visual changes to the event site usually mean editing files under `sites/totalverplant/`.

---

## Python / Tooling Conventions

- Use **`uv`** for Python dependency and tooling commands.
- Run tests via:

```bash
uv run pytest tests
```

- Code style:
  - 80‑column line length
  - Single quotes for strings
  - Ruff for formatting and linting

Common commands:

```bash
# Install dependencies (including dev + test)
uv sync --group dev --group test

# Format
uv run ruff format .

# Lint
uv run ruff check .

# Type check
uv run mypy byceps
```

For native, non-Docker commands, BYCEPS usually needs a config file via
`BYCEPS_CONFIG_FILE`, e.g. `BYCEPS_CONFIG_FILE=config/config.toml uv run byceps`.

---

## Database & Data

Configuration (Docker) is in `docker/byceps/config.toml`:

- Host: `db`
- Port: `5432`
- User: `byceps`
- DB: `byceps`

Typical flows:

- **Initialize empty schema**:

  ```bash
  docker compose exec byceps-apps uv run byceps initialize-database
  ```

- **Load a SQL dump into the local DB** (from repo root, with Docker running):

  ```bash
  # Place dump under ./data/, e.g. data/byceps-stage.sql
  docker compose exec -T db psql -U byceps -d byceps < data/byceps-stage.sql
  ```

- **Full clean restore from dump** (destructive!):

  ```bash
  # Terminate connections and recreate database
  docker compose exec -T db psql -U byceps -d postgres -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'byceps' AND pid <> pg_backend_pid();"

  docker compose exec -T db psql -U byceps -d postgres -c "DROP DATABASE IF EXISTS byceps;"
  docker compose exec -T db psql -U byceps -d postgres -c "CREATE DATABASE byceps;"

  # Load dump
  docker compose exec -T db psql -U byceps -d byceps < data/byceps-stage.sql
  ```

---

## Architecture Notes

- Multi‑application architecture:
  - Admin app (`byceps/blueprints/admin.py`)
  - Site app (`byceps/blueprints/site.py`)
  - API app (`byceps/blueprints/api.py`)
  - CLI app (`app.py`)
  - Worker (RQ)
- Dispatcher (`byceps/web_apps_dispatcher.py`, used by `serve_web_apps.py`)
  routes by `Host` header.
- Service‑layer pattern:
  - Domain services under `byceps/services/<domain>/`
  - `dbmodels/` use SQLAlchemy
  - `models/` use Pydantic/dataclasses

When modifying behavior, prefer adding/changing service functions rather than mixing persistence/business logic directly in views.

---

## Repository Layout (High Level)

- `byceps/` – application code
  - `blueprints/` – Flask blueprints (admin, site, api)
  - `services/` – domain services (`authn`, `authz`, `user`, `ticketing`, `shop`, `seating`, `board`, `news`, `tourney`, `party`, `brand`, `site`, ...)
  - `static/` – shared static files (CSS, JS, images)
- `sites/` – per‑site overrides
  - `cozylan/`
  - `totalverplant/`
- `config/` – example/native configuration (`config.toml.example`)
- `docker/` – Docker‑specific config
  - `byceps/config.toml` – Docker config used by `BYCEPS_CONFIG_FILE`
  - `nginx/` – nginx includes/templates
- `docs/` – Sphinx documentation (published to Read the Docs)
- `tests/` – pytest test suite
  - `unit/`
  - `integration/`
- `scripts/` – maintenance and helper scripts
- `assets/` – images and static assets used by docs/README

---

## Testing Layout & Commands

- Tests live under `tests/`:
  - `tests/unit/` – fast, isolated unit tests
  - `tests/integration/` – integration tests (often DB‑backed)

Common commands:

```bash
# All tests
uv run pytest tests

# Single test file
uv run pytest tests/unit/services/shop/order/test_order_sequence.py

# Single test
uv run pytest tests/unit/services/shop/order/test_order_sequence.py::test_generate_order_number
```

For substantial changes, prefer:

- Adding or updating **unit tests** first.
- Using **integration tests** when behavior crosses service boundaries or requires DB state.

---

## Docs & Internationalization

- Docs are in `docs/` and published to:
  - `https://byceps.readthedocs.io/en/latest/`
- Building docs locally:

```bash
just docs-build-html      # or:
cd docs && uv run make html
```

i18n commands (see `justfile`):

```bash
just babel-extract
just babel-init <locale>
just babel-compile
```

---

## Expectations for AI Agents

When making changes:

1. **Bias for action**, but:
   - Do **not** rewrite large swaths of the codebase without a clear request.
   - Prefer small, well‑scoped commits.
2. **Respect existing patterns**:
   - Use existing services instead of ad‑hoc SQL where possible.
   - Reuse helper functions and utilities rather than duplicating logic.
3. **Be careful with migrations**:
   - BYCEPS does not use Alembic.
   - Schema changes require manual migration steps; do not silently change models without calling this out.
4. **Testing**:
   - For non‑trivial changes, add or update tests under `tests/`.
   - Run relevant tests locally when feasible.

Git safety:

- Do **not** change Git configuration.
- Do **not** force‑push `main`/`master`.
- Only create commits or push when explicitly requested.

---

## Team Contribution Conventions

When contributing as part of a team, prefer changes that are easy to review,
easy to test, and easy to revert.

- Keep PRs **small and single-purpose**. Avoid mixing refactors, formatting,
  bug fixes, and feature work in one change.
- Preserve existing behavior unless the PR explicitly intends to change it.
  If behavior changes, call that out clearly in the PR description.
- Prefer **following local patterns** over introducing a new style or structure.
  Consistency is usually more valuable than novelty.
- Do not rename, move, or reorganize files without a clear reason. Those
  changes create review noise and make history harder to follow.
- Avoid opportunistic cleanup in unrelated areas. If you notice a separate
  issue, mention it separately instead of bundling it into the same PR.
- Do not include drive-by refactors, dependency updates, or formatting-only
  churn unless they are part of the requested change.
- Add or update tests for non-trivial changes. If you cannot add tests, explain
  why and describe how the change was verified.
- For non-trivial code changes, AI contributors should add or update tests in
  the same PR.
- Prefer writing tests before or alongside the implementation, not as an
  afterthought.
- Run only the relevant checks for the touched area when possible, then report
  what you ran and what you did not run.
- Include a short verification summary in the PR or handoff note, e.g. tests
  run, lint/format checks run, and any manual UI checks performed.
- Before preparing a PR, run the relevant tests for the touched area and, when
  feasible, the relevant formatting and lint checks.
- If tests were not added or could not be run, state that explicitly and
  explain why.
- Keep commits and PR descriptions **why-focused**:
  - What problem is being solved?
  - What changed?
  - How was it verified?
  - Are there follow-up tasks or known limitations?
- Leave code and templates easier to understand:
  - Prefer clear names over clever ones.
  - Prefer small, local changes over broad rewrites.
  - Add short comments only when intent would otherwise be unclear.
- Be careful with shared files such as config, layout templates, and common
  services. Small edits there can have wide impact across the project.
- For visual/site changes, include enough context for reviewers to validate the
  result quickly, e.g. affected page, template, or screenshots if appropriate.
- If you are unsure whether a change is safe, ask for guidance before expanding
  scope. A narrow PR with an explicit question is better than a broad PR with
  hidden risk.
- If a change proposes a schema update, the PR must include explicit manual
  migration steps. BYCEPS does not use Alembic.

---

## Branching & PR Workflow (Suggested)

- **Main branch**:
  - `main` is treated as stable; do not commit directly unless explicitly asked.

- **Feature branches**:
  - Create topic branches from `main`, e.g. `feature/totalverplant-homepage`, `fix/shop-order-email`.
  - Keep changes focused and reasonably small.

- **Branch naming**:
  - Prefer the format `<type>/<area>-<change>`.
  - Use short, descriptive, lowercase names with hyphens.
  - Good prefixes include `feature/`, `fix/`, `docs/`, `refactor/`, `test/`, and `chore/`.
  - Name the intended outcome, not the activity.
  - Examples:
    - `feature/totalverplant-homepage-cta`
    - `fix/ticketing-checkin-filter`
    - `docs/update-agents-guidance`
  - Avoid vague names like `misc-fixes`, `updates`, or personal names.

- **Before opening a PR** (or asking an AI to prepare one):
  - Ensure the app starts cleanly with `docker compose up -d`.
  - Run at least the relevant tests (unit and/or integration) for the touched areas.
  - Format and lint:

    ```bash
    uv run ruff format .
    uv run ruff check .
    ```

- **Commits**:
  - Prefer clear, “why‑focused” messages (e.g. `Improve homepage ticket CTA for totalverplant`, not `misc fixes`).
  - Avoid bundling unrelated changes into a single commit.

- **PR descriptions**:
  - Keep them short, concrete, and review-friendly.
  - Prefer this structure:
    - Problem: what issue is being solved?
    - Change: what was changed?
    - Verification: what tests/checks/manual validation were performed?
    - Risks / Follow-ups: any known limitations, migration steps, or open questions

- **UI/site changes**:
  - Mention the affected page, template, or route.
  - State whether desktop/mobile behavior was checked.
  - Include screenshots when the visual change is significant or not obvious
    from the diff alone.

---

## How to Extend This File

When new conventions or workflows emerge (e.g. a new site, a new deployment flow, or AI‑specific rules), prefer:

- Adding a **short, focused section** here instead of creating scattered tool‑specific docs.
- Keeping this file up to date when workflows change.

This keeps humans and all AI agents aligned on how to work effectively in this repo.
