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
- **Description**: Generates the bracket, the groups or the lobbies from the draft. The only entry for initial generation, for regenerating the playoffs after their release and for creating a later FFA round. Refused while the draft has problems or is stale. Consumes the draft version: a second submit with the same version is refused with the flash 'changed by another orga'. When the matches already follow the current code, nothing is regenerated (notice flash)

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
- **URL**: `/<tournament_id>/matches`
- **Method**: GET
- **Permission**: `lan_tournament.view`
- **Description**: Lists all matches for a tournament with contestants
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/matches`

#### View Match
- **URL**: `/lan-tournaments/matches/<match_id>`
- **Method**: GET
- **Permission**: `lan_tournament.view`
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
- **URL**: `/lan-tournaments/matches/<match_id>/confirm`
- **Method**: POST
- **Permission**: `lan_tournament.administrate`
- **Description**: Confirms a match result as final
- **Example**: `/lan-tournaments/matches/abcdef01-2345-6789-abcd-ef0123456789/confirm`

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
- **Description**: Lists all matches for a tournament with contestants and scores
- **Example**: `/lan-tournaments/01234567-89ab-cdef-0123-456789abcdef/matches`

#### View Match
- **URL**: `/lan-tournaments/matches/<match_id>`
- **Method**: GET
- **Authentication**: Not required
- **Description**: Shows detailed match information including contestants, scores, and comments
- **Example**: `/lan-tournaments/matches/abcdef01-2345-6789-abcd-ef0123456789`

#### View Bracket
- **URL**: `/lan-tournaments/<tournament_id>/bracket`
- **Method**: GET
- **Authentication**: Not required
- **Description**: Displays tournament bracket visualization for public viewing
- **Example**: `/lan-tournaments/01234567-89ab-cdef-0123-456789abcdef/bracket`

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
| `/lan-tournaments/orga/tournaments/<tournament_id>/advance_ffa_round` | POST | Advance FFA Round (Draft) |
| `/lan-tournaments/orga/tournaments/<tournament_id>/generate_ffa_grand_final` | POST | Generate FFA Grand Final |

---

## API Endpoints

Currently, there are no dedicated REST API endpoints. All interactions happen through the web interface routes documented above.

---

## Permission Requirements

### Admin Permissions
- `lan_tournament.view` - View tournaments, teams, and matches
- `lan_tournament.create` - Create tournaments and teams
- `lan_tournament.update` - Update tournaments, teams, and match scores
- `lan_tournament.delete` - Delete tournaments and teams
- `lan_tournament.administrate` - Full control including status changes, bracket generation, match confirmation
- `lan_tournament.maintain` - Party maintenance: delete unused images and orphaned image files

### User Authentication
- Most site URLs are publicly viewable (no authentication)
- Actions like joining, leaving, and team creation require `@login_required`
- Draft tournaments are hidden from site visitors

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

**✅ Correct:**
- `/lan-tournaments/...` (both admin and site use this prefix)
