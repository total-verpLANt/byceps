# Participant chair information

This service lets the current participant of each party ticket state whether
they will bring their own chair or need a provided chair.

## Participant rules

- A chair answer belongs to the ticket's current `used_by` participant.
- Ticket ownership and seat or user management rights do not grant access.
- Tickets without seats can be answered.
- A seat change preserves the answer.
- A ticket-user change makes the previous answer stale. The new participant
  starts with no valid answer and replaces the ticket-level record when they
  submit one.

## Administration

The party-specific `More` page links to `Seat management`. Its first tool is
`Participant chair information`, which provides summary counts, a participant
table, CSV export, and graphical seating plans. The participant table can be
filtered by answer state or missing seat and links to the corresponding user,
ticket, and seat views.

The graphical seating plans show the participant and chair status in each
occupied seat's tooltip. A green inner outline and dot mark participants who
bring their own chair.

The overview and export use the existing `seating.view` permission. There is no
Chair-specific permission or role.

The three answer-state summary values partition all non-revoked party tickets
with a current participant. `No seat` is an additional count over the same
tickets, so a ticket without a seat is also counted in exactly one answer state.

## Persistence

Answers are stored in `party_ticket_chair_optouts`, with one row per party and
ticket. The row stores the participant ID that supplied the current answer.
Reads treat a row as valid only while its `user_id` matches the ticket's current
`used_by_id`.

The three states are:

- no valid row for the current participant: not specified yet;
- `brings_own_chair = true`: brings own chair;
- `brings_own_chair = false`: needs a provided chair.

Seat labels and seating-plan positions are resolved from the current Seating
data and are not duplicated in the Chair table.

## Validation

Use the repository commands documented in the root `justfile`, `pyproject.toml`,
and CI workflow. Chair tests are located below these paths:

- `tests/unit/blueprints/chair_optout`;
- `tests/unit/services/chair_optout`;
- `tests/integration/blueprints/admin/chair_optout`;
- `tests/integration/blueprints/site/chair_optout`;
- `tests/integration/services/chair_optout`.
