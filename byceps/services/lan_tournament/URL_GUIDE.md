# LAN Tournament Module - URL Guide

This guide documents all available URLs for the LAN Tournament module, covering both administrative and user-facing routes.

## URL Prefixes

- **Admin URLs**: Mounted under `/lan-tournaments`
- **Site URLs**: Mounted under `/lan-tournaments`

**Note:** The module uses dash (`lan-tournaments`) not underscore in URLs to follow Flask/HTTP conventions.

## Admin URLs

### Tournament Management

#### List Tournaments for Party
- **URL**: `/lan-tournaments/for_party/<party_id>`
- **Method**: GET
- **Permission**: `lan_tournament.view`
- **Description**: Lists all tournaments for a specific party/event
- **Example**: `/lan-tournaments/for_party/lanparty-2024-q1`

#### View Tournament
- **URL**: `/lan-tournaments/tournaments/<tournament_id>`
- **Method**: GET
- **Permission**: `lan_tournament.view`
- **Query**: `return=<encoded dashboard list query>` (optional, allowlisted list parameters only, see Orga Dashboard Reference). A holder of `lan_tournament.administrate` gets a link back to the dashboard list; everyone else gets the page unchanged. The value is read and never a redirect target
- **Description**: Shows details of a specific tournament
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef`

#### Create Tournament Form
- **URL**: `/lan-tournaments/for_party/<party_id>/create`
- **Method**: GET
- **Permission**: `lan_tournament.create`
- **Description**: Displays form to create a new tournament
- **Example**: `/lan-tournaments/for_party/lanparty-2024-q1/create`

#### Create Tournament (Submit)
- **URL**: `/lan-tournaments/for_party/<party_id>`
- **Method**: POST
- **Permission**: `lan_tournament.create`
- **Description**: Processes tournament creation form submission
- **Form Fields**:
  - `name`: Tournament name (required)
  - `game`: Game name
  - `description`: Tournament description
  - `image_url`: Image URL
  - `ruleset`: Rules description
  - `start_time`: Start time (datetime)
  - `contestant_type`: SOLO or TEAM
  - `tournament_mode`: SINGLE_ELIMINATION, etc.
  - `min_players`, `max_players`: Player limits
  - `min_teams`, `max_teams`: Team limits (for team tournaments)
  - `min_players_in_team`, `max_players_in_team`: Team size limits

#### Create Wizard: Upload Image
- **URL**: `/lan-tournaments/for_party/<party_id>/create/image`
- **Endpoint**: `lan_tournament_admin.upload_create_image`
- **Method**: POST (`multipart/form-data`, file field `image`)
- **Permission**: `lan_tournament.create`
- **Description**: Validates, re-encodes and stores a tournament image for the party. The request body is capped at `MAX_REQUEST_BYTES` (5 MiB + 256 KiB), the file at 5 MiB.
- **Success**: `201` with `{image_id, url, filename, width, height, byte_size}`; `url` is the relative served path.
- **Errors** (JSON `{error: <translated message>}`): `400` no file or unreadable/too small/too large image, `413` body or file above the limit, `415` not JPEG/PNG/WebP. An unknown party is a plain `404`.

#### Create Wizard: Delete Staged Image
- **URL**: `/lan-tournaments/for_party/<party_id>/create/image/<image_id>`
- **Endpoint**: `lan_tournament_admin.delete_create_image`
- **Method**: DELETE
- **Permission**: `lan_tournament.create`
- **Description**: Deletes an unreferenced image, only for its uploader and only within the party it was uploaded for.
- **Success**: `204`, empty body.
- **Errors** (JSON `{error}`): `404` unknown image, malformed UUID or image of another party, `403` not the uploader, `409` referenced by a tournament.
- **Client URL**: build with `url_for('lan_tournament_admin.delete_create_image', party_id=..., image_id='__ID__')`; the client replaces `__ID__` with the image id.

#### Create Wizard: List Images
- **URL**: `/lan-tournaments/for_party/<party_id>/images`
- **Endpoint**: `lan_tournament_admin.list_create_images`
- **Method**: GET
- **Permission**: `lan_tournament.create`
- **Query**: `scope=party|brand` (default `party`), `q` (file name filter, cut to 100 characters), `page` (1 to 10000, default 1). Anything else is `400`.
- **Success**: `200` with `{items: [{image_id, url, filename, width, height, byte_size, party_title, used_by: [tournament names], created_at (ISO 8601)}], page, has_next}`. Brand scope covers the parties of the party's brand only.

#### Create Wizard: Pre-Check
- **URL**: `/lan-tournaments/for_party/<party_id>/create/validate`
- **Endpoint**: `lan_tournament_admin.validate_create`
- **Method**: POST (same form body as the create submit, without the image file)
- **Permission**: `lan_tournament.create`
- **Description**: Runs the create validation without writing anything: no tournament, no flash, no request unlink.
- **Success**: `200` with `{ok, errors: {<field>: [messages]}, first_error_step, checked_at}`. Form-level errors are under the key `""`.

#### Import Tournament Form
- **URL**: `/lan-tournaments/for_party/<party_id>/import`
- **Endpoint**: `lan_tournament_admin.import_form`
- **Method**: GET
- **Permission**: `lan_tournament.create`
- **Description**: Displays the upload form for a tournament configuration file (see Tournament Configuration File). The page links back to the tournament list of the party.
- **Success**: `200`. An unknown party is a plain `404`.

#### Import Tournament (Check or Create)
- **URL**: `/lan-tournaments/for_party/<party_id>/import`
- **Endpoint**: `lan_tournament_admin.import_config`
- **Method**: POST (`multipart/form-data`)
- **Permission**: `lan_tournament.create`
- **Query Parameters**: `step=import`, set by the form of the summary page (step `import`); the form of the first step sends none. It only picks the message of a `413` (see Errors) and changes nothing else
- **Form Fields**:
  - `action`: `check` or `import` (required)
  - `config_file`: the uploaded document (step `check`)
  - `config_document`: the checked document as base64, carried in a hidden field by the summary page (step `import`). An upload takes precedence over it
  - `submission_token`: UUID that makes a repeated submit idempotent (step `import`)
  - `image`, `image_alt_text`: optional tournament image and its description, at most 200 characters (step `import`); the image is not part of the document
- **Description**: `action=check` is the dry run: it parses and checks the document like the create wizard does and writes nothing (no tournament, no image, no log entry). `action=import` checks the document again in full and creates a new tournament with status `DRAFT` in the party, in one transaction, with an audit entry for the import (document SHA-256 and format version). The admin does not select the file a second time. The summary page shows the start time of the file in the viewer's time zone; it is changed afterwards in the edit form.
- **Success**: `check`: `200` with the summary page. `import`: `302` to the new tournament (admin view) with a flash. A repeated submit with the same `submission_token` creates no second tournament and redirects to the first with a notice.
- **Errors**: a missing or unknown `action` is a plain `400` (before the document is checked). Document, rule or image problems are `200` with the form page again and at most 20 problems listed, and write nothing: no file, a bad base64 carry or a document that fails the check or the wizard rules. A request body above the budget (5 MiB image, the document as base64 and 64 KiB for the other fields: 5,657,944 bytes) is `413` with the same page and one problem. With `step=import` the problem blames the image, the only large part of the second step: "Tournament image: The file is <size>. The maximum is 5 MB.". Without it, or with any other value, the problem is "The file is larger than 256 KiB.". An unknown party is a plain `404`.

#### Export Tournament Configuration
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/export`
- **Endpoint**: `lan_tournament_admin.export_config`
- **Method**: POST (a GET is `405`: the export writes an audit entry)
- **Permission**: `lan_tournament.view`
- **Description**: Downloads the configuration document of the tournament (see Tournament Configuration File) and records the export in the audit log (document SHA-256 and format version).
- **Success**: `200`, `application/json` as an attachment named `lan-tournament-<slug>-<YYYYMMDD>.json` (ASCII only: the tournament name is transliterated, at most 40 characters of slug, `tournament` if nothing is left; the date is the day of the export in UTC), with `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.
- **Errors**: `403` without the permission, `404` for an unknown tournament.

#### Update Tournament Form
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/update`
- **Method**: GET
- **Permission**: `lan_tournament.update`
- **Description**: Displays form to update tournament details
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/update`

#### Update Tournament (Submit)
- **URL**: `/lan-tournaments/tournaments/<tournament_id>`
- **Method**: POST
- **Permission**: `lan_tournament.update`
- **Description**: Processes tournament update form submission
- **Form Fields**: Same as create form

#### Delete Tournament
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/delete`
- **Method**: POST
- **Permission**: `lan_tournament.delete`
- **Description**: Deletes a tournament and all associated data
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/delete`

### Tournament Status Management

#### Open Registration
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/open_registration`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Changes status to REGISTRATION_OPEN, allowing participants to join

#### Close Registration
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/close_registration`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Changes status to REGISTRATION_CLOSED, no more participants allowed

#### Start Tournament
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/start`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Form Fields**: `confirm_generated_layout` (optional; required when the seeding board changed after the bracket was generated, so the start uses the generated layout)
- **Description**: Changes status to ONGOING, tournament is now active. A tournament without a seeding draft (generated before the seeding board existed) starts without a confirmation

#### Pause Tournament
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/pause`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Changes status to PAUSED, temporarily halts tournament

#### Resume Tournament
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/resume`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Form Fields**: `confirm_generated_layout` (required when a REGISTRATION_CLOSED tournament is started through this route and the seeding board differs from the generated layout; not required for an ordinary PAUSED resume)
- **Description**: Changes status back to ONGOING from PAUSED

#### Complete Tournament
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/complete`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Changes status to COMPLETED, tournament finished

#### Cancel Tournament
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/cancel`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Changes status to CANCELLED, tournament aborted

#### Generate Bracket
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/generate_bracket`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Generates nothing. Redirects to the seeding board (`.../seeding`); generation runs from the seeding draft only (see Seeding and Playoffs (Admin))
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/generate_bracket`

### Seeding and Playoffs (Admin)

All routes: `lan_tournament.administrate`. A seeding target is `initial` (default), `playoff` (the playoff draft) or `ffa:<SE|WB|LB>:<round>` (a later FFA round). A seeding write carries the `version` it was made against; a stale version is answered with 409. A request that sends `Accept: application/json` gets JSON (`board` or `qualification`, errors as `{error}`), any other request flashes and redirects.

#### Seeding Board
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/seeding`
- **Method**: GET
- **Query**: `target` (default `initial`)
- **Description**: Shows the seeding draft: seed code, roster, problems, orga actions and the audit entries (`seeding-*`, `bracket-*`, `qualification-*`, `playoffs-*`). The initial board opens once registration is closed and stays readable after the start
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/seeding?target=playoff`

#### Seeding Action
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/seeding/actions`
- **Method**: POST
- **Form Fields**: `version`, `target`, `action` and its arguments:
  - `swap`: `p`, `q` (positions)
  - `move_tier`: `contestant_id`, `tier`, optional `ref_id`, `after` (FFA only)
  - `set_tier_count`: `n` (2 to 4, FFA only)
  - `replay`: `code` (a seed code of the same mode)
  - `redraw`, `reset_fixes`, `reseed_keep_tiers`, `separate`: no arguments
- **Description**: Applies one orga action to the draft and answers with the new board. A malformed request is 422, a stale `version` 409

#### Generate From Seeding
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/seeding/generate`
- **Method**: POST
- **Form Fields**: `version`, `target`
- **Description**: Generates the bracket, the groups or the lobbies from the draft. The only entry for initial generation, for regenerating the playoffs after their release and for creating a later FFA round. Refused while the draft has problems or is stale. Refused for the initial target while a match with two contestants has a confirmed result. Consumes the draft version: a second submit with the same version is refused with the flash 'changed by another orga'. When the matches already follow the current code, nothing is regenerated (notice flash)

#### Qualification
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/qualification`
- **Method**: GET
- **Description**: Shows who qualifies for the playoffs (group standings or leaderboard), the ties that block the release, the playoff draft and the release controls

#### Qualification: Playoff Draft Action
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/qualification/draft`
- **Method**: POST
- **Form Fields**: same as Seeding Action; the target is always `playoff`
- **Description**: Applies one seeding action to the playoff draft

#### Qualification: Create Playoff Draft
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/qualification/draft/create`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Creates the prefilled playoff draft once the qualification is ready and none exists. Logs `seeding-drawn` with the acting user

#### Qualification: Decisions
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/qualification/decisions`
- **Method**: POST
- **Form Fields**: `scope` (a group, or `ffa:<pool>:<round>:<n>`), `action` (`save` or `withdraw`), `order` (repeated, required for both actions: the decided order for `save`, the IDs of the decision block to withdraw for `withdraw`), `reason`, `back` (FFA scopes only: `bracket` or `ffa_standings`, the page the decision returns to; anything else returns to the bracket)
- **Description**: Saves or withdraws an orga decision on a tie. Locked after the release, except for FFA scopes

#### Qualification: Release
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/qualification/release`
- **Method**: POST
- **Form Fields**: `version` (of the playoff draft)
- **Description**: Generates the playoff phase from the playoff draft. Refused while a tie blocks the qualification

#### Qualification: Unrelease
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/qualification/unrelease`
- **Method**: POST
- **Form Fields**: `reason`
- **Description**: Takes the release back and removes the playoff matches. Refused once a playoff match has a result

#### Close Leaderboard
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/leaderboard/close`
- **Method**: POST
- **Description**: Ends the score phase of a highscore tournament, so its qualification can be decided

#### Reopen Leaderboard
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/leaderboard/reopen`
- **Method**: POST
- **Form Fields**: `reason`
- **Description**: Reopens a closed highscore qualification for score submission and correction while ongoing or paused. Undo the playoff release first

#### Advance FFA Round (Draft)
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/advance_ffa_round`
- **Method**: POST
- **Form Fields**: `pool` (`WB` or `LB`, double elimination only)
- **Description**: Drafts the next FFA round and redirects to its seeding board (`.../seeding?target=ffa:...`); the lobbies are generated from the draft, never here. Works for FFA tournaments and for an FFA playoff phase

#### Generate FFA Grand Final
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/generate_ffa_grand_final`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Generates the grand final lobby of a double-elimination FFA phase (FFA tournament or HS->FFA playoffs) once the service gate allows it; audited as `bracket-generated`

### Team Management (Admin)

#### List Teams for Tournament
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/teams`
- **Method**: GET
- **Permission**: `lan_tournament.view`
- **Description**: Lists all teams participating in a tournament
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/teams`

#### Create Team Form
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/teams/create`
- **Method**: GET
- **Permission**: `lan_tournament.create`
- **Description**: Displays form to create a new team
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/teams/create`

#### Create Team (Submit)
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/teams`
- **Method**: POST
- **Permission**: `lan_tournament.create`
- **Description**: Processes team creation form submission
- **Form Fields**:
  - `name`: Team name (required)
  - `tag`: Team tag/abbreviation
  - `description`: Team description
  - `image_url`: Team logo URL
  - `join_code`: Join code for team access (optional, will be hashed)

#### Update Team Form
- **URL**: `/lan-tournaments/teams/<team_id>/update`
- **Method**: GET
- **Permission**: `lan_tournament.update`
- **Description**: Displays form to update team details
- **Example**: `/lan-tournaments/teams/fedcba98-7654-3210-fedc-ba9876543210/update`

#### Update Team (Submit)
- **URL**: `/lan-tournaments/teams/<team_id>`
- **Method**: POST
- **Permission**: `lan_tournament.update`
- **Description**: Processes team update form submission
- **Form Fields**: Same as create team form

#### Delete Team
- **URL**: `/lan-tournaments/teams/<team_id>/delete`
- **Method**: POST
- **Permission**: `lan_tournament.delete`
- **Description**: Deletes a team and removes all members
- **Example**: `/lan-tournaments/teams/fedcba98-7654-3210-fedc-ba9876543210/delete`

### Participant Management (Admin)

#### List Participants for Tournament
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/participants`
- **Method**: GET
- **Permission**: `lan_tournament.view`
- **Description**: Lists all participants with ticket status and team assignment
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/participants`

#### Add Participant Form
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/participants/add`
- **Method**: GET
- **Permission**: `lan_tournament.administrate`
- **Description**: Displays form to add a participant by screen name (admin only, no ticket check)
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/participants/add`

#### Add Participant (Submit)
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/participants/add`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Adds a participant by screen name lookup. Skips ticket validation. Works during REGISTRATION_OPEN and REGISTRATION_CLOSED. Reactivates soft-deleted participants if they previously left.
- **Form Fields**:
  - `screen_name`: User's screen name (required)

#### Remove Participant
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/participants/<participant_id>/remove`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Removes a single participant from the tournament
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/participants/fedcba98-7654-3210-fedc-ba9876543210/remove`

#### Remove Participants Without Tickets
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/participants/remove_without_tickets`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Bulk removes all participants who lack valid party tickets. In team tournaments, transfers captain roles before removal. During ONGOING status, uses soft-delete to preserve match history.
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/participants/remove_without_tickets`

### Match Management (Admin)

#### List Matches for Tournament
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/matches`
- **Method**: GET
- **Permission**: `lan_tournament.view`
- **Query**: `only` (optional): readiness bucket, default `all` (see Match List Filter)
- **Description**: Lists the matches of a tournament with contestants and readiness status. The filter tabs and their counts are tournament-wide and equal to the site list
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/matches?only=partially_ready`

#### View Match
- **URL**: `/lan-tournaments/matches/<match_id>`
- **Method**: GET
- **Permission**: `lan_tournament.view`
- **Query**: `return=<encoded dashboard list query>` (optional, allowlisted list parameters only, see Orga Dashboard Reference). A holder of `lan_tournament.administrate` gets a link back to the dashboard list, anchored to the row of this match (`#lt-row-<match_id>`); everyone else gets the page unchanged. The value is read and never a redirect target
- **Description**: Shows detailed match information including contestants, scores, and comments
- **Example**: `/lan-tournaments/matches/abcdef01-2345-6789-abcd-ef0123456789`

#### Set Match Score
- **URL**: `/lan-tournaments/matches/<match_id>/set_score`
- **Method**: POST
- **Permission**: `lan_tournament.update`
- **Description**: Sets the score for a contestant in a match
- **Form Fields**:
  - `contestant_id`: UUID of participant or team
  - `score`: Integer score value
- **Example**: `/lan-tournaments/matches/abcdef01-2345-6789-abcd-ef0123456789/set_score`

#### Confirm Match
- **URL**: `/lan-tournaments/matches/<match_id>/confirm_with_scores`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Sets the scores of all contestants and confirms the match result as final. The comment is stored together with the confirmation in one transaction and marked in the comment history as a confirmation by the orga. A missing or blank comment refuses the confirmation and changes nothing
- **Form Fields**:
  - `score_<key>`: Integer score, one per contestant (`<key>` is the team ID or participant ID)
  - `comment`: Comment text (required)
- **Example**: `/lan-tournaments/matches/abcdef01-2345-6789-abcd-ef0123456789/confirm_with_scores`

#### Confirm FFA Match
- **URL**: `/lan-tournaments/matches/<match_id>/confirm_ffa`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Confirms a free-for-all match after its placements were set (`/lan-tournaments/matches/<match_id>/set_ffa_placements`). Same comment rule as Confirm Match
- **Form Fields**:
  - `comment`: Comment text (required)
- **Example**: `/lan-tournaments/matches/abcdef01-2345-6789-abcd-ef0123456789/confirm_ffa`

#### Add Match Comment
- **URL**: `/lan-tournaments/matches/<match_id>/add_comment`
- **Method**: POST
- **Permission**: `lan_tournament.update`
- **Description**: Adds a comment to a match
- **Form Fields**:
  - `comment`: Comment text (required)
- **Example**: `/lan-tournaments/matches/abcdef01-2345-6789-abcd-ef0123456789/add_comment`

#### Delete Match Comment
- **URL**: `/lan-tournaments/matches/<match_id>/comments/<comment_id>/delete`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Deletes a comment from a match
- **Example**: `/lan-tournaments/matches/abcdef01-2345-6789-abcd-ef0123456789/comments/11111111-2222-3333-4444-555555555555/delete`

#### View Bracket (Admin)
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/bracket`
- **Method**: GET
- **Permission**: `lan_tournament.view`
- **Description**: Displays tournament bracket visualization for administrators
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/bracket`

#### Party Maintenance (Admin)
- **URLs**:
  - `/lan-tournaments/for_party/<party_id>/maintenance` (GET): the actions with counts
  - `/lan-tournaments/for_party/<party_id>/maintenance/<action_id>` (GET): preview of what the action would delete
  - `/lan-tournaments/for_party/<party_id>/maintenance/<action_id>` (POST): execute the action
- **Permission**: `lan_tournament.maintain`
- **Actions**: `unused-images`, `orphaned-files`; an unknown action is 404
- **Form field** (POST): repeatable `key`, one per selected item; no keys flashes "Nothing selected." and redirects to the preview
- **Rules**: only this party; items younger than 24 h are kept; the server rechecks every posted key; at most 100 keys count per POST; one log record per run (party, actor, action, count, bytes, keys)
- **Result**: flash with the numbers (deleted, skipped, failed file deletes); a run that deletes nothing flashes "Nothing was deleted." instead of the success line; image skips are split into "a tournament uses it now" and "no longer qualifies"; redirect to the maintenance tab
- **Example**: `/lan-tournaments/for_party/lan-2026/maintenance/unused-images`

#### Party Maintenance: Dashboard Thresholds (Admin)
- **URL**: `/lan-tournaments/for_party/<party_id>/maintenance/dashboard-thresholds`
- **Endpoint**: `lan_tournament_admin.update_dashboard_thresholds`
- **Method**: POST. The card is part of the maintenance tab (`GET /lan-tournaments/for_party/<party_id>/maintenance`, anchor `#dashboard-thresholds`). The static rule wins over `POST .../maintenance/<action_id>`; `dashboard-thresholds` is not an action id
- **Permission**: `lan_tournament.maintain`
- **Form Fields** (each exactly once; a duplicate counts as missing):
  - `csrf_token`: dashboard token of the viewer (see Orga Dashboard Reference)
  - `action`: `save` or `reset`
  - `expected_revision`, `expected_updated_at`: the version the card was rendered from. `0` and an empty value while the party has no override; otherwise the revision and `YYYY-MM-DDTHH:MM:SS.ffffff` (naive UTC)
  - `yellow_minutes`, `red_minutes` (`save` only): 1 to 9 ASCII digits each. The service then requires whole minutes with `1 <= yellow < red <= 1440`
- **Description**: Saves or removes the override of the yellow and red thresholds of this party. It applies to every tournament of the party, on the admin and the site dashboard. Poll interval and page size always come from the deployment configuration. The party comes from the URL; a `party_id` form field is never read. A change is checked against the version (revision and time) the card showed, so a second orga's change in between is refused, never overwritten. The service logs party, actor, old and new values to the application log
- **Response**: `303` to the maintenance tab with a flash ("The dashboard thresholds were saved." / "... were reset to the default."). A reset without an override is a success
- **Errors**: without the `maintain` permission (also anonymous) the core `403` page, nothing is stored and no service runs. Otherwise the tab is rendered again with the submitted values and version, and the marker `data-error-code`: `403` `csrf_invalid` (missing, foreign, stale or duplicated token); `422` `invalid` (unknown `action`, unreadable or out-of-bounds minutes, with a field error); `409` `stale` (the version is no longer current or does not parse; sending the same form again stays `409` until the tab is reloaded). There is no JSON variant
- **Broken deployment configuration**: the tab stays `200` and the card shows the error instead of the form
- **Example**: `/lan-tournaments/for_party/lan-2026/maintenance/dashboard-thresholds`

### Orga Dashboard (Admin)

The due matches of a party across its tournaments, for holders of `lan_tournament.administrate`. A scoped orga without that permission is refused here and uses the site dashboard. The party always comes from the URL. Every answer carries `Cache-Control: private, no-store`. Query parameters, JSON answers, error codes, the form contract and the `return=` context are described once in the Orga Dashboard Reference; the site routes behave the same way (see Orga Dashboard (Site)).

The party tab "Dashboard" (right after "Übersicht") links the list. It is shown only to holders of `lan_tournament.administrate`.

#### Dashboard List
- **URL**: `/lan-tournaments/for_party/<party_id>/dashboard`
- **Endpoint**: `lan_tournament_admin.dashboard_for_party`
- **Method**: GET
- **Permission**: `lan_tournament.administrate`. Anonymous: redirect to the login form. Authenticated without the permission: `403`. Unknown party: `404`
- **Query**: `scope`, `view`, `state`, `sort`, `tournament`, `page`
- **Description**: One page of the party's matches, with tier tiles, filters and the shared pin and acknowledgement forms. `scope=assigned` (default) shows the tournaments the viewer is assigned to; `scope=all` shows every tournament of this party and works only for this permission. The request is read-only: no INSERT, UPDATE or DELETE, no row lock, no clock initialisation
- **Example**: `/lan-tournaments/for_party/lan-2026/dashboard?scope=all&view=due&sort=wait`

#### Dashboard Poll
- **URL**: `/lan-tournaments/for_party/<party_id>/dashboard/poll`
- **Endpoint**: `lan_tournament_admin.dashboard_poll_for_party`
- **Method**: GET
- **Permission**: `lan_tournament.administrate`, answered as JSON and never as a redirect: anonymous (and a user without `admin.access`, which core reads as anonymous) gets `401` `session_expired`; an authenticated user without the permission gets `403` `access_revoked`
- **Query**: the same as the list
- **Success**: `200` `{html, as_of, poll_seconds}`: the rendered panel, the ISO 8601 UTC time of the snapshot and the refresh interval in seconds. An unknown party is an empty list, not a `404`
- **Example**: `/lan-tournaments/for_party/lan-2026/dashboard/poll?scope=all&view=all&sort=wait`

#### Pin Match
- **URL**: `/lan-tournaments/for_party/<party_id>/dashboard/matches/<match_id>/pin`
- **Endpoint**: `lan_tournament_admin.dashboard_pin`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`; the service checks the authority again under the tournament lock
- **Form Fields**: `csrf_token`, `revision`, `pinned` (`true` or `false`), `return`
- **Description**: Sets the shared pin of a live match to an explicit state for every orga with authority. The match must belong to the party of the URL
- **Response**: see Orga Dashboard Reference (native `303`, JSON `200`, refusals)

#### Acknowledge Delay
- **URL**: `/lan-tournaments/for_party/<party_id>/dashboard/matches/<match_id>/ack`
- **Endpoint**: `lan_tournament_admin.dashboard_ack`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`; the service checks the authority again under the tournament lock
- **Form Fields**: `csrf_token`, `episode`, `revision`, `comment` (optional), `return`
- **Description**: Records that an orga checked the delay of a due match, for the current wait episode only. It restarts the alert interval, not the total wait
- **Response**: see Orga Dashboard Reference (native `303`, JSON `200`, refusals)

---

## Site URLs (User-Facing)

### Tournament Discovery and Viewing

#### List All Tournaments
- **URL**: `/lan-tournaments/` (site-specific base path)
- **Method**: GET
- **Authentication**: Not required
- **Description**: Lists all visible tournaments for the current party (excludes drafts). Sorted by registration status and start time.
- **Example**: `/lan-tournaments/`

#### View Tournament
- **URL**: `/lan-tournaments/<tournament_id>`
- **Method**: GET
- **Authentication**: Not required
- **Query**: `return=<encoded dashboard list query>` (optional, allowlisted list parameters only, see Orga Dashboard Reference). An orga with at least one assignment in the current party gets a link back to the dashboard list; everyone else gets the page unchanged. The value is read and never a redirect target
- **Description**: Shows tournament details, participants list, and join/leave options. Hides draft tournaments.
- **Example**: `/lan-tournaments/01234567-89ab-cdef-0123-456789abcdef`

### Tournament Participation

#### Join Tournament
- **URL**: `/lan-tournaments/<tournament_id>/join`
- **Method**: POST
- **Authentication**: Required (`@login_required`)
- **Description**: Allows user to register as participant in a tournament (only during REGISTRATION_OPEN status)
- **Example**: `/lan-tournaments/01234567-89ab-cdef-0123-456789abcdef/join`

#### Leave Tournament
- **URL**: `/lan-tournaments/<tournament_id>/leave`
- **Method**: POST
- **Authentication**: Required (`@login_required`)
- **Description**: Allows user to unregister from a tournament (only during REGISTRATION_OPEN status)
- **Example**: `/lan-tournaments/01234567-89ab-cdef-0123-456789abcdef/leave`

### Team Management (User)

#### List Teams
- **URL**: `/lan-tournaments/<tournament_id>/teams`
- **Method**: GET
- **Authentication**: Not required
- **Description**: Lists all teams for a tournament
- **Example**: `/lan-tournaments/01234567-89ab-cdef-0123-456789abcdef/teams`

#### Create Team Form
- **URL**: `/lan-tournaments/<tournament_id>/teams/create`
- **Method**: GET
- **Authentication**: Required (`@login_required`)
- **Description**: Displays form for creating a new team (only during REGISTRATION_OPEN)
- **Example**: `/lan-tournaments/01234567-89ab-cdef-0123-456789abcdef/teams/create`

#### Create Team (Submit)
- **URL**: `/lan-tournaments/<tournament_id>/teams/create`
- **Method**: POST
- **Authentication**: Required (`@login_required`)
- **Description**: Processes team creation. User becomes captain automatically.
- **Form Fields**:
  - `name`: Team name (required)
  - `tag`: Team tag
  - `description`: Team description
  - `join_code`: Join code for team (optional, will be hashed)
- **Example**: `/lan-tournaments/01234567-89ab-cdef-0123-456789abcdef/teams/create`

#### View Team
- **URL**: `/lan-tournaments/teams/<team_id>`
- **Method**: GET
- **Authentication**: Not required
- **Description**: Shows team details, members list, and join/leave options
- **Example**: `/lan-tournaments/teams/fedcba98-7654-3210-fedc-ba9876543210`

#### Join Team
- **URL**: `/lan-tournaments/teams/<team_id>/join`
- **Method**: POST
- **Authentication**: Required (`@login_required`)
- **Description**: Allows a tournament participant to join a team
- **Requirements**:
  - Must be registered in tournament first
  - Not already in another team
  - Registration must be open
- **Form Fields**:
  - `join_code`: Required if team has a join code
- **Example**: `/lan-tournaments/teams/fedcba98-7654-3210-fedc-ba9876543210/join`

#### Leave Team
- **URL**: `/lan-tournaments/teams/<team_id>/leave`
- **Method**: POST
- **Authentication**: Required (`@login_required`)
- **Description**: Allows a team member to leave their team (only during REGISTRATION_OPEN)
- **Example**: `/lan-tournaments/teams/fedcba98-7654-3210-fedc-ba9876543210/leave`

### Match Viewing

#### List Matches
- **URL**: `/lan-tournaments/<tournament_id>/matches`
- **Method**: GET
- **Authentication**: Not required
- **Query**: `only` (optional): readiness bucket, default `all` (see Match List Filter)
- **Description**: Lists the matches of a tournament with contestants, scores and readiness status. The filter bar counts are tournament-wide. "Meine Partien" is a client-side toggle on the list, not a query parameter
- **Example**: `/lan-tournaments/01234567-89ab-cdef-0123-456789abcdef/matches?only=not_ready`

#### View Match
- **URL**: `/lan-tournaments/matches/<match_id>`
- **Method**: GET
- **Authentication**: Not required
- **Query**: `return=<encoded dashboard list query>` (optional, allowlisted list parameters only, see Orga Dashboard Reference). An orga with at least one assignment in the current party gets a link back to the dashboard list, anchored to the row of this match (`#lt-row-<match_id>`); everyone else gets the page unchanged. The value is read and never a redirect target
- **Description**: Shows detailed match information including contestants, scores, and comments
- **Example**: `/lan-tournaments/matches/abcdef01-2345-6789-abcd-ef0123456789`

#### View Bracket
- **URL**: `/lan-tournaments/<tournament_id>/bracket`
- **Method**: GET
- **Authentication**: Not required
- **Description**: Displays tournament bracket visualization for public viewing
- **Example**: `/lan-tournaments/01234567-89ab-cdef-0123-456789abcdef/bracket`

### Match Readiness (Site)

Each side of a one-versus-one match reports itself ready or not ready. Both routes take the same guarded form; there is no reason field and no readiness history.

#### Claim Readiness
- **URL**: `/lan-tournaments/matches/<match_id>/ready/claim`
- **Method**: POST
- **Authentication**: Required (`@login_required`)
- **Authorization**: Checked per side by the service: the participant of a solo side or the captain of a team side may act for their own side; a tournament orga or a holder of `lan_tournament.administrate` may act for either side. Anyone else gets 403
- **Form Fields** (all exactly once):
  - `csrf_token`: readiness token of the viewer (invalid or missing: 403)
  - `side`: `a` or `b`
  - `expected_pairing_generation`: pairing generation shown on the card
  - `expected_readiness_revision`: readiness revision shown on the card
- **Description**: Marks one side ready ("Ich bin bereit", orga: "Bereit (für diese Seite)"). Only while the tournament is ONGOING and the match has a valid current pairing. A stale generation or revision is refused with a flash. Writes the audit entry `match-ready-claimed`
- **Response**: Redirect to View Match with the flash "Readiness claimed."
- **Example**: `/lan-tournaments/matches/abcdef01-2345-6789-abcd-ef0123456789/ready/claim`

#### Revoke Readiness (Un-ready)
- **URL**: `/lan-tournaments/matches/<match_id>/ready/revoke`
- **Method**: POST
- **Authentication**: Required (`@login_required`)
- **Authorization**: Same as Claim Readiness
- **Form Fields**: Same as Claim Readiness. No `reason` field: a side just reports itself not ready
- **Description**: Withdraws the readiness of one side ("Ich bin nicht bereit", orga: "Nicht bereit (für diese Seite)"). Writes the audit entry `match-ready-revoked`; the match keeps no revocation record
- **Response**: Redirect to View Match with the flash "Readiness revoked."
- **Example**: `/lan-tournaments/matches/abcdef01-2345-6789-abcd-ef0123456789/ready/revoke`

The admin has no readiness routes: the admin match view shows the state of both sides read-only.

### Orga Match Actions (Site)

Tournament orgas (and global administrators) enter, confirm and retract match results on the site. All routes need `@login_required` and `@scoped_orga_required` (403 for anyone who may not administrate the tournament of the match) and work only while the tournament is ONGOING. A confirmation needs a comment written in the same submission: the comment is stored together with the confirmation in one transaction and marked in the comment history as a confirmation by the orga. A missing or blank comment refuses the confirmation and changes nothing. The comment is separate from the `reason` of a correction or an unconfirm.

#### Confirm Match With Scores (Orga)
- **URL**: `/lan-tournaments/orga/matches/<match_id>/confirm_with_scores`
- **Method**: POST
- **Authentication**: Required (`@login_required`, `@scoped_orga_required`)
- **Form Fields**:
  - `score_<key>`: Integer score, one per contestant (`<key>` is the team ID or participant ID)
  - `comment`: Comment text (required)
- **Description**: Sets the scores of all contestants and confirms a bracket match. The button sits in the comment form of the match page; the score inputs are in the orga panel
- **Example**: `/lan-tournaments/orga/matches/abcdef01-2345-6789-abcd-ef0123456789/confirm_with_scores`

#### Submit FFA Result (Orga)
- **URL**: `/lan-tournaments/orga/matches/<match_id>/submit_ffa_result`
- **Method**: POST
- **Authentication**: Required (`@login_required`, `@scoped_orga_required`)
- **Form Fields**:
  - `placement_<key>`: Integer placement, one per contestant (`<key>` is the team ID or participant ID)
  - `comment`: Comment text (required)
- **Description**: Sets the placements of a free-for-all match and confirms them in one step. Refused for bracket matches
- **Example**: `/lan-tournaments/orga/matches/abcdef01-2345-6789-abcd-ef0123456789/submit_ffa_result`

#### Unconfirm Match (Orga)
- **URL**: `/lan-tournaments/orga/matches/<match_id>/unconfirm`
- **Method**: POST
- **Authentication**: Required (`@login_required`, `@scoped_orga_required`)
- **Form Fields**:
  - `reason`: Reason text (required; not the confirm comment)
- **Description**: Retracts the result of a free-for-all match. Bracket matches are refused and are retracted through Correct Match Result
- **Example**: `/lan-tournaments/orga/matches/abcdef01-2345-6789-abcd-ef0123456789/unconfirm`

#### Correct Match Result (Orga)
- **URL**: `/lan-tournaments/orga/matches/<match_id>/correct_result`
- **Method**: POST
- **Authentication**: Required (`@login_required`, `@scoped_orga_required`)
- **Form Fields**:
  - `reason`: Reason text (required; not the confirm comment)
  - `corrected_score_<key>`: Integer score, one per contestant (all blank: retract only)
  - `ack_critical`, `ack_match_ids`: Acknowledgement of the impact on confirmed downstream matches
- **Description**: Retracts the result of a bracket match and optionally confirms new scores. Refused for free-for-all matches
- **Example**: `/lan-tournaments/orga/matches/abcdef01-2345-6789-abcd-ef0123456789/correct_result`

**Comments**: `/lan-tournaments/orga/matches/<match_id>/add_comment` was removed. The match page has one comment section, and orgas post to `/lan-tournaments/matches/<match_id>/add_comment` like contestants do (field `comment`); a scoped orga may comment in every tournament status, a contestant only while the tournament is ONGOING.

### Orga Seeding and Playoffs (Site)

Tournament orgas (and global administrators) run the seeding flow on the site. All routes need `@login_required` and `@scoped_orga_required`; the behaviour, form fields, JSON answers and status codes match the admin routes of the same name (see Seeding and Playoffs (Admin)).

| Route | Method | Admin counterpart |
|-------|--------|-------------------|
| `/lan-tournaments/orga/tournaments/<tournament_id>/seeding` | GET | Seeding Board |
| `/lan-tournaments/orga/tournaments/<tournament_id>/seeding/actions` | POST | Seeding Action |
| `/lan-tournaments/orga/tournaments/<tournament_id>/seeding/generate` | POST | Generate From Seeding |
| `/lan-tournaments/orga/tournaments/<tournament_id>/qualification` | GET | Qualification |
| `/lan-tournaments/orga/tournaments/<tournament_id>/qualification/draft` | POST | Playoff Draft Action |
| `/lan-tournaments/orga/tournaments/<tournament_id>/qualification/draft/create` | POST | Create Playoff Draft |
| `/lan-tournaments/orga/tournaments/<tournament_id>/qualification/decisions` | POST | Decisions |
| `/lan-tournaments/orga/tournaments/<tournament_id>/qualification/release` | POST | Release |
| `/lan-tournaments/orga/tournaments/<tournament_id>/qualification/unrelease` | POST | Unrelease |
| `/lan-tournaments/orga/tournaments/<tournament_id>/leaderboard/close` | POST | Close Leaderboard |
| `/lan-tournaments/orga/tournaments/<tournament_id>/leaderboard/reopen` | POST | Reopen Leaderboard (`reason` required) |
| `/lan-tournaments/orga/tournaments/<tournament_id>/advance_ffa_round` | POST | Advance FFA Round (Draft) |
| `/lan-tournaments/orga/tournaments/<tournament_id>/generate_ffa_grand_final` | POST | Generate FFA Grand Final |

### Orga Dashboard (Site)

Scoped orgas read and act on the due matches of their assigned tournaments on the site. The viewer needs a login and at least one orga assignment in the party of the current site; the party is the site's and never comes from the request. A global administrator without an assignment gets `403` here (the admin dashboard is theirs); with assignments they are held to them on the site too, for reads and for pin and acknowledgement. The routes use their own collection check, not `@scoped_orga_required`. The static rule `/lan-tournaments/orga-dashboard` wins over `/lan-tournaments/<tournament_id>`. Every answer carries `Cache-Control: private, no-store`. Behaviour, query parameters, JSON answers and error codes match the admin routes of the same role and are described in the Orga Dashboard Reference.

| Route | Method | Guard | Admin counterpart |
|-------|--------|-------|-------------------|
| `/lan-tournaments/orga-dashboard` | GET | Anonymous: login redirect. Logged in without an assignment in this party: `403` page | Dashboard List |
| `/lan-tournaments/orga-dashboard/poll` | GET | JSON only: `401` `session_expired` (anonymous), `403` `access_revoked` (no assignment); never a redirect | Dashboard Poll |
| `/lan-tournaments/orga-dashboard/matches/<match_id>/pin` | POST | Own check chain, below | Pin Match |
| `/lan-tournaments/orga-dashboard/matches/<match_id>/ack` | POST | Own check chain, below | Acknowledge Delay |

Endpoints: `lan_tournament.orga_dashboard`, `orga_dashboard_poll`, `orga_dashboard_pin`, `orga_dashboard_ack`. `scope` is ignored on the site: the scope is always the viewer's assignments, re-resolved on every request. Form fields of the POST routes are the same as in the admin section.

**Check chain of a POST**, in this fixed order: session (JSON `401` `session_expired`; native: login redirect), collection (no assignment: `403` `access_revoked`), dashboard CSRF token (`403` `csrf_invalid`), match lookup (one `404` `unavailable` for a malformed ID, a missing match, another party's match and a match outside the viewer's assignments), form validation (`422` `invalid`), then the service, which checks authority, revision and episode again under the tournament lock. A revoked viewer with a valid token reads `access_revoked`; a viewer with authority and a bad token reads `csrf_invalid`.

**Discoverability**: the site index (generic and `totalverplant-36`) shows an "Orga-Dashboard" button to a logged-in viewer with an assignment. It uses the same check as the routes, so link and route cannot disagree. Admin: the party tab "Dashboard", see Orga Dashboard (Admin).

---

## Orga Dashboard Reference

One canonical, party-scoped list of matches, on two surfaces (admin page and site page) that read through the same service. For one scope and one moment, page and poll agree on rows, order, tiles and counts.

### Query parameters (list and poll)

Every value is validated on the server. An invalid, repeated or out-of-scope value is dropped to that field's default with a notice; it is never repaired and never widens the scope. A malformed query never causes a `500`. Unknown parameters are ignored on the list.

| Parameter | Values (first is the default) | Meaning |
|-----------|-------------------------------|---------|
| `scope` | `assigned`, `all` | Admin only; the site ignores it. `all` is every tournament of the party and works only with `lan_tournament.administrate`; anything else resolves to the viewer's assignments in this party |
| `view` | `due`, `upcoming`, `all` | `due`: matches with current demand (state due, or unknown timing). `upcoming`: not due yet (later round, partial pairing, lobby not complete). `all`: every state, including paused, bye and done rows |
| `state` | `all`, `tier-red`, `tier-yellow`, `tier-green`, `ready-none`, `ready-one`, `ready-both`, `ready-unavailable`, `conflict`, `review-open`, `pinned` | Filter inside the view; the tier tiles toggle `tier-*` |
| `sort` | `urgency`, `wait`, `tournament` | `urgency`: due first, then tier red, yellow, green, then conflict or open review, then alert interval and total wait, descending |
| `tournament` | a tournament UUID | Must lie inside the resolved scope. A hidden, a foreign and a missing ID give the same notice ("not available") |
| `page` | `1` to `1000000` | The page size comes from the deployment configuration, never from the query |

### What the list means

- **Due**: a match is due when its demand is real and current: a complete pairing that can be played now in an `ONGOING` tournament. Single and double elimination: the playable pairing. Round robin: the earliest unfinished round per phase and group. Free-for-all: the lobby of the current generated round. Later rounds, partial pairings, incomplete lobbies, byes and confirmed matches are not due. Pre-start and paused time do not count.
- **Active wait**: the tournament's operational clock runs only while the tournament is `ONGOING`. It starts at the real start (`REGISTRATION_CLOSED` to `ONGOING`), freezes on a pause and continues on resume. A paused tournament's matches show in view `all` only and cannot be acknowledged.
- **Alert interval and tiers**: the alert interval of a due match is the active time since its wait episode opened or since the latest acknowledgement, whichever is later. Green below the yellow threshold, yellow from it, red from the red threshold (defaults 15 and 45 minutes, per party overridable in the Wartung tab). The total active wait stays visible separately.
- **Acknowledgement**: records the acting orga, the time and an optional comment as a checked delay, not as ownership or resolution. It is shared, bound to the current episode and revision, and restarts the alert interval, so the match escalates again after the yellow and red intervals. Only yellow and red can be acknowledged. A new episode (for example after a corrected result reopens the match) starts without inherited acknowledgements.
- **Pin**: a shared flag on a live match, set and removed explicitly by any orga with authority; a pin disappears with its match.
- **Readiness**: the per-side Ready state of the match is displayed; it never starts, resets or pauses the wait.
- **Last change**: the domain-only time of the last change of a match. Comments, pins and acknowledgements never move it.
- **Conflicts**: the same person demanded by two different due matches of the same party. A viewer sees the named counterparts they may see. A counterpart outside the viewer's tournaments is disclosed only by one generic, fixed sentence naming the person ("... is needed in a match outside your tournaments at the same time."), with no tournament, match, team, time, count or link, and identical for one or many hidden overlaps. That generic disclosure is the only authorized look beyond the assignments. Another party's demand is never a conflict.
- **Unknown history**: a tournament that already ran when the schema was applied has no operational clock, and a match without a stored last-change time has none either. Such timing is shown as unavailable and never reconstructed; no acknowledgement can be recorded for a tournament without a clock (`refused`). See `migrations/README.md`, migration 023.

### JSON answers and error codes

The answer is JSON when the poll route is called, or when a POST sends `Accept: application/json` (`application/json` must be the preferred type). Without it, a POST is answered natively.

- **Poll `200`**: `{html, as_of, poll_seconds}`
- **POST success, JSON `200`**: `{committed_at, fragment}` where `committed_at` is the server time of the write (ISO 8601 UTC) and `fragment` is the re-read poll body
- **POST success, native**: a flash and `303` to the list URL rebuilt from the route, the validated `return` query and the row anchor `#lt-row-<match_id>`
- **Refusal, JSON**: `{error, message}` plus, for `stale`, `refused` and `invalid`, `fragment` (the whole re-read panel) and `draft_target` (the target row is in that panel and still offers the action). `message` is final localized text. `session_expired`, `access_revoked`, `unavailable` and `csrf_invalid` carry neither
- **Refusal, native**: the dashboard page again with the status of the table, the draft kept and the form reopened; `401` goes to the login form, a hidden or missing match gets the one `404` page

| `error` | Status | When |
|---------|--------|------|
| `session_expired` | `401` | no session (poll and JSON POST; a native request is redirected to the login form) |
| `access_revoked` | `403` | no dashboard authority now (permission or assignment), also when lost between the gate and the service |
| `csrf_invalid` | `403` | missing, malformed, foreign or stale token |
| `unavailable` | `404` | malformed ID, missing match, another party's match, a match outside the viewer's scope: one answer for all |
| `stale` | `409` | pin revision or acknowledgement episode/revision is not the current one, including a duplicate submit |
| `refused` | `409` | match or tournament is terminal, tournament paused, match not due, clock unknown, alert interval below the yellow threshold, or acknowledged recently |
| `invalid` | `422` | a hidden field or the comment fails validation; `message` is the form's own text |

A client dispatches on `error`, not on the status alone. A non-JSON `5xx` (for example the operator error below) is transient.

Known limits of the refresh script: every successful poll replaces the whole panel, so a text selection or a screen reader's reading position is lost each interval. A POST that times out in the browser (15 s) reads "not saved" although the server may have committed; the next submit then gets `stale`, which is safe. The JSON path of a stale acknowledgement shows the message only; the native path also names the actor and time of the other orga's record.

### Form contract of pin and acknowledgement

All fields are untrusted and must be sent exactly once. The server never takes the party, the scope or any authority from the form.

- `csrf_token`: the module's own token, bound to the session and the user and kept in the signed session. Validation never mints or rotates it. `LocalizedForm` carries no CSRF token of its own, so these forms and the Wartung threshold form use this one
- `revision`: unsigned decimal, at most 2147483647: the pin revision (pin) or the acknowledgement revision of the episode (acknowledge) the form was rendered from
- `pinned` (pin): exactly `true` or `false`; there is no toggle and no default
- `episode` (acknowledge): the canonical hyphenated UUID of the due episode the form was rendered from
- `comment` (acknowledge, optional): plain text of at most 500 characters. CRLF becomes LF and the text is trimmed; blank means none; control characters other than newline and tab, line and paragraph separators, surrogates and bidirectional controls are refused
- `return`: the list context to come back to (below); at most 1024 characters, an over-long value is dropped, not refused

### The `return=` context

The list URL a viewer came from is carried as `return=<urlencoded query string>` to the tournament and match pages (admin and site) and as the hidden `return` field of the pin and acknowledgement forms. It is an allowlisted context, never a URL and never a redirect target.

- It is accepted only if it is exactly an encoded query string (strict parse) of the allowlisted parameters `scope`, `view`, `state`, `sort`, `tournament` and `page`, each at most once, with valid values, at most 1024 characters. Any other value (an absolute or scheme-relative URL, a path, markup, an unknown or repeated key, a bad pair or escape, an over-long value) falls back to the default list. No `return` at all means no link
- The destination page rebuilds the link with `url_for` from its own tournament's party and the validated query; the raw value is neither echoed nor used for the party, and the site ignores `scope`
- It is never a redirect target: the native `303` after a pin or acknowledgement goes to the same rebuilt URL, and no route redirects to the raw value
- Authority: the admin pages show the link only to holders of `lan_tournament.administrate`; the site pages only to an orga with an assignment in the current party. Without authority the page is byte-identical with and without `return`. A match page adds the row anchor `#lt-row-<match_id>`; a tournament page has none

### Configuration

Deployment settings come from the environment (`LAN_TOURNAMENT_DASHBOARD_YELLOW_MINUTES`, `_RED_MINUTES`, `_POLL_SECONDS`, `_PAGE_SIZE`); defaults, bounds and precedence are in `migrations/README.md` (migration 023). The Wartung card overrides only the two thresholds per party. A set but invalid deployment value is an operator error: the dashboard routes answer `500`, the admin back link is hidden and the Wartung card shows the error instead of its form (a destination page never fails), and nothing falls back silently.

### Cost

Measured on a synthetic party of 33 tournaments and 1,368 fixtures (30 round robins of 10 players, 2 single eliminations, 1 free-for-all), no latency target is derived from it. The dashboard snapshot runs 11 statements with conflicts (9 for `view=upcoming`, 6 for a page past the end), under the budget of 16; a whole request, including session, scope and layout, runs 18 to 23. Growth from 10 to 50 to 100 rows and from 1 to 33 tournaments is 0 statements. The party-wide due oracle runs three times (counts, rows, conflicts), so the cost follows the fixtures of the party, not the page: about 70 ms for the rows and 150 to 230 ms for the conflicts statement at that size. Look at the plans before a party grows far past a few thousand fixtures.

---

## Match List Filter

The site list (`/lan-tournaments/<tournament_id>/matches`) and the admin list (`/lan-tournaments/tournaments/<tournament_id>/matches`) take the same `only` query parameter. The tournament page links to both lists with it. Every match is in exactly one bucket, so the bucket counts add up to `all`. Counts and lists use the whole tournament, never the viewer's own matches.

| `only` | Matches in the bucket | Label (de) |
|--------|-----------------------|------------|
| `waiting` | No complete pairing yet (fewer than two contestants assigned), not finished | Wartet auf Gegner |
| `not_ready` | Two contestants assigned, neither side ready | Nicht bereit |
| `partially_ready` | Exactly one side ready | Teilweise bereit |
| `both_ready` | Both sides ready | Beide bereit |
| `no_readiness` | Two or more contestants in a format without per-side readiness (for example free-for-all), not finished. The option is listed only while its count is above 0 | Offen (ohne Bereitschaft) |
| `finished` | Confirmed result or defwin, or the tournament is completed or cancelled | Beendet |
| `all` (default) | Every match | Alle |

The legacy values `ready`, `playable` and `open`, an empty value and any unknown value fall back to `all`; a stale link never hides a match.

---

## Match Invitations

Each recipient of a match pairing has one work item per pairing generation. A mail that the SMTP server definitely rejects, or that fails to reach the queue, is retried automatically: up to 3 attempts in total, 30 seconds after the first and 120 seconds after the second. When a tournament starts or resumes (status ONGOING), a catch-up reconciles every match, recovers expired leases and sends the `pending` or `failed` work that is due, at most 100 items, and never repeats an `accepted` mail. A permanent failure (mail configuration or build error) or an exhausted retry cycle stays `failed`, and an ambiguous outcome stays `delivery_unknown`. As with every BYCEPS mail, a mail that failed stays failed: no admin or site route resends invitations.

---

## Tournament Configuration File

The create settings of one tournament as a JSON file, to move a setup between BYCEPS instances. The export route writes it and the import routes read it (see Admin URLs); both are admin-only, there is no site route.

```json
{
  "format": "lan_tournament.config",
  "version": 1,
  "tournament": {
    "name": "Rocket League 2v2",
    "category": "MAIN",
    "start_time": "2026-10-09T18:00:00Z",
    "contestant_type": "TEAM",
    "game_format": "ONE_V_ONE",
    "elimination_mode": "SINGLE_ELIMINATION",
    "max_teams": 16
  }
}
```

### Envelope

- `format`: must be the text `lan_tournament.config`; anything else is not a tournament configuration file.
- `version`: a whole number that must equal `1`. The check is strict equality: an older or newer version is refused as unsupported (and nothing else in the file is checked), there is no conversion between versions.
- `tournament`: an object with the keys below. An unknown key, in the envelope or in `tournament`, is refused; a key that appears twice is refused.

### Keys of `tournament`

The 27 keys are the fields of `TournamentConfigInput` (`tournament_config_domain_service.py`). `name` and `category` are required; every other key may be left out, and a missing or `null` value is treated like an untouched field of the create wizard.

| Kind | Keys | Rule |
|------|------|------|
| Required text | `name`, `category` | `null` is refused; `category` is an enum name (below) |
| Text or `null` | `game`, `description`, `ruleset` | Length limits and trimming as in the create wizard |
| Time or `null` | `start_time` | ISO 8601 with a UTC offset, at most 64 characters. The export writes UTC with a trailing `Z`; the import accepts any offset and stores UTC. A time without an offset is refused |
| Enum name or `null` | `contestant_type`, `game_format`, `elimination_mode`, `score_ordering`, `playoff_elimination_mode`, `playoff_release_mode` | The member name, never the label: `category` (required) `MAIN`, `FUN`, `STAGE`, `USER_ORGANIZED`; `contestant_type` `SOLO`, `TEAM`; `game_format` `ONE_V_ONE`, `FREE_FOR_ALL`, `HIGHSCORE`; `elimination_mode` and `playoff_elimination_mode` `SINGLE_ELIMINATION`, `DOUBLE_ELIMINATION`, `ROUND_ROBIN`, `NONE`; `score_ordering` `HIGHER_IS_BETTER`, `LOWER_IS_BETTER`; `playoff_release_mode` `AUTOMATIC`, `MANUAL` |
| Whole number or `null` | `min_players`, `max_players`, `min_teams`, `max_teams`, `min_players_in_team`, `max_players_in_team`, `advancement_count`, `group_size_min`, `group_size_max`, `playoff_group_count`, `playoff_qualifiers_per_group`, `playoff_qualifier_count` | A JSON integer; a float, text or boolean is refused. Ranges are the create wizard's |
| List of whole numbers or `null` | `point_table` | At most 64 places |
| Flag | `points_carry_to_losers`, `playoff_enabled` | `true` or `false`; `null` counts as `false` |

### Reading rules

- The file is UTF-8 text with one JSON object, at most 256 KiB (262,144 bytes). `NaN` and `Infinity` are refused, and so is text with a NUL or an unpaired surrogate.
- After parsing, the values go through the same normaliser as the create wizard, so every rule the wizard enforces applies to an import: lengths, count ranges, the cross-field rules between game format, contestant type, elimination mode and playoffs, and the playoff gating.
- Problems are reported per key, at most 20 on the page.

### Never exported

The file holds only the 27 keys above. It never carries identifiers (`id`, `party_id`), timestamps, the creating request, the creation token, the status (an import always creates a `DRAFT`), the position, the image (`image_id`, `image_url`, `image_alt_text`: an image is attached in the import step), winner and playoff release state, leaderboard close, operational clock, `use_bracket_reset`, or anything that belongs to a running tournament (participants, teams, matches, seedings, scores, comments, log entries).

---

## API Endpoints

Currently, there are no dedicated REST API endpoints. All interactions happen through the web interface routes documented above.

---

## Permission Requirements

### Admin Permissions
- `lan_tournament.view` - View tournaments, teams, and matches; export the configuration file of a tournament
- `lan_tournament.create` - Create tournaments and teams; import a tournament from a configuration file (form, check and create). Scoped site orgas can neither import nor export
- `lan_tournament.update` - Update tournaments, teams, and match scores
- `lan_tournament.delete` - Delete tournaments and teams
- `lan_tournament.administrate` - Full control including status changes, bracket generation, match confirmation, and the admin orga dashboard (list, poll, pin, acknowledge)
- `lan_tournament.maintain` - Party maintenance: delete unused images and orphaned image files, and set or reset the dashboard thresholds of the party

### User Authentication
- Most site URLs are publicly viewable (no authentication)
- Actions like joining, leaving, and team creation require `@login_required`
- Draft tournaments are hidden from site visitors
- The site orga dashboard (`/lan-tournaments/orga-dashboard` and its poll, pin and acknowledge routes) needs a login and an orga assignment in the party of the current site; `lan_tournament.administrate` alone is not enough there

---

## URL Parameter Types

- `party_id`: String identifier for party/event (e.g., `lanparty-2024-q1`)
- `tournament_id`: UUID (e.g., `01234567-89ab-cdef-0123-456789abcdef`)
- `team_id`: UUID (e.g., `fedcba98-7654-3210-fedc-ba9876543210`)
- `match_id`: UUID (e.g., `abcdef01-2345-6789-abcd-ef0123456789`)
- `comment_id`: UUID (e.g., `11111111-2222-3333-4444-555555555555`)

---

## Typical User Workflows

### Admin: Creating and Running a Tournament
1. Create tournament → `/lan-tournaments/for_party/<party_id>/create`
2. Open registration → `/lan-tournaments/tournaments/<tournament_id>/open_registration`
3. (Optional) Add participants manually → `/lan-tournaments/tournaments/<tournament_id>/participants/add`
4. Close registration → `/lan-tournaments/tournaments/<tournament_id>/close_registration`
5. (Optional) Add late participants → `/lan-tournaments/tournaments/<tournament_id>/participants/add` (works after registration closes)
6. Seed and generate → `/lan-tournaments/tournaments/<tournament_id>/seeding` (then `/seeding/generate`; with playoffs: `/qualification`, then release)
7. Start tournament → `/lan-tournaments/tournaments/<tournament_id>/start`
8. Manage matches → `/lan-tournaments/matches/<match_id>`
9. Complete tournament → `/lan-tournaments/tournaments/<tournament_id>/complete`

### Orga: Watching the Dashboard
1. Scoped orga (site): open the "Orga-Dashboard" button on `/lan-tournaments/` → `/lan-tournaments/orga-dashboard`
2. Administrator (admin): open the party tab "Dashboard" → `/lan-tournaments/for_party/<party_id>/dashboard` (add `scope=all` for every tournament of the party)
3. Open a match or tournament from a row; the page carries `return=` and offers the way back to the same list and row
4. Pin a match, or acknowledge a yellow or red delay with an optional comment; the other orgas see both on their next poll
5. Change the thresholds of the party (Maintenance tab, permission `lan_tournament.maintain`) → `/lan-tournaments/for_party/<party_id>/maintenance/dashboard-thresholds`

### User: Solo Tournament Participation
1. View tournaments → `/lan-tournaments/`
2. View specific tournament → `/lan-tournaments/<tournament_id>`
3. Join tournament → `/lan-tournaments/<tournament_id>/join`
4. View matches → `/lan-tournaments/<tournament_id>/matches`
5. View bracket → `/lan-tournaments/<tournament_id>/bracket`

### User: Team Tournament Participation
1. View tournaments → `/lan-tournaments/`
2. Join tournament → `/lan-tournaments/<tournament_id>/join`
3. Create team → `/lan-tournaments/<tournament_id>/teams/create`
4. Share team ID with teammates
5. Teammates join team → `/lan-tournaments/teams/<team_id>/join`
6. View matches → `/lan-tournaments/<tournament_id>/matches`

---

## Common Mistakes and Conventions

### URL Naming Convention
- **Python module name**: `lan_tournament` (uses underscore)
- **URL paths**: `lan-tournaments` (uses dash/hyphen)

This follows common web conventions where:
- Underscores are used in code/identifiers
- Dashes/hyphens are used in URLs (better for readability and SEO)

### Frequently Confused URLs

**❌ Wrong:**
- `/admin/lan_tournament/...` (wrong prefix and underscore)
- `/tournaments/...` (missing "lan-" prefix)
- `/lan_tournament/...` (underscore instead of dash)
- `/lan-tournaments/orga_dashboard` (the site route is `/lan-tournaments/orga-dashboard`, with a hyphen; the endpoint is `lan_tournament.orga_dashboard`)
- `/lan-tournaments/orga-dashboard?party_id=...` (the site party is never a parameter; the admin party is `/lan-tournaments/for_party/<party_id>/dashboard`)

**✅ Correct:**
- `/lan-tournaments/...` (both admin and site use this prefix)
