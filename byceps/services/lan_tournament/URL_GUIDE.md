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
- **Description**: Changes status to ONGOING, tournament is now active

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
- **Description**: Generates single-elimination bracket with matches based on registered contestants
- **Example**: `/lan-tournaments/tournaments/01234567-89ab-cdef-0123-456789abcdef/generate_bracket`

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

### Match Management (Admin)

#### List Matches for Tournament
- **URL**: `/lan-tournaments/tournaments/<tournament_id>/matches`
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
3. Close registration → `/lan-tournaments/tournaments/<tournament_id>/close_registration`
4. Generate bracket → `/lan-tournaments/tournaments/<tournament_id>/generate_bracket`
5. Start tournament → `/lan-tournaments/tournaments/<tournament_id>/start`
6. Manage matches → `/lan-tournaments/matches/<match_id>`
7. Complete tournament → `/lan-tournaments/tournaments/<tournament_id>/complete`

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
