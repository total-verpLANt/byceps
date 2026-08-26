"""Authenticated native readiness requests through the real Flask apps.

Real signed-cookie sessions of logged-in users, the decorated site routes, the
real services and PostgreSQL. Every refused request must have written nothing:
the match row, the invitation work, the audit log and the pairing history are
compared through a connection of their own, and the post-commit effects
(signals and queueing) must not have fired.
"""

from datetime import datetime, UTC
import html as htmllib
import re
import secrets
from types import SimpleNamespace
from urllib.parse import quote, urljoin

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_invitation_service as invitations,
    tournament_match_service as engine,
    tournament_orga_service as orgas,
    tournament_readiness_service as readiness,
    tournament_repository as repo,
    tournament_service as lifecycle,
)
from byceps.services.lan_tournament.blueprints.readiness_csrf import (
    READINESS_CSRF_SESSION_KEY,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.match_readiness import (
    DbMatchInvitation,
    DbMatchPairing,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7

from tests.helpers import http_client, log_in_user


SITE_HOST = 'http://www.acmecon.test'
SITE_ROOT = f'{SITE_HOST}/lan-tournaments'
ENGLISH = {'Accept-Language': 'en'}

# The real service operations, captured before any test spies on them.
REAL_CLAIM = readiness.claim_ready_flush
REAL_REVOKE = readiness.revoke_ready_flush

A, B = MatchSide.A, MatchSide.B

# Fields no readiness form defines. None of them may ever widen authority.
FORGED = {
    'is_orga': 'true',
    'orga': '1',
    'role': 'orga',
    'actor_role': 'orga',
    'on_behalf': '1',
    'permission': 'lan_tournament.administrate',
    'permissions': '*',
}


class _Drop:
    """Marker: remove this field from the form."""


DROP = _Drop()


# -- sessions, forms and requests --


def armed(actor, token):
    """The session value a rendered page would have stored for the actor."""
    return {'user_id': str(actor.id), 'token': token}


def nothing(actor, token):
    return DROP


def _flashes(client):
    with client.session_transaction() as session:
        flashes = session.get('_flashes', [])
    messages = [
        message
        if isinstance(message, dict)
        else {'text': str(message), 'category': None}
        for _, message in flashes
    ]
    return [message.get('text') for message in messages]


def make_form(
    token, world, target, *, route, generation=None, revision=None, **extra
):
    """The posted form for `target` side; `extra` overrides or drops fields."""
    current_generation, current_revision = revisions(world)
    data = {
        'csrf_token': token,
        'side': target.value,
        'expected_pairing_generation': str(
            current_generation if generation is None else generation
        ),
        'expected_readiness_revision': str(
            current_revision if revision is None else revision
        ),
    }
    data.update(extra)
    return {key: value for key, value in data.items() if value is not DROP}


def post(app, actor, path, form, *, stored=armed, host=SITE_ROOT):
    """POST as the logged-in actor; `form` is built from the session token."""
    token = secrets.token_urlsafe(32)
    with http_client(app, user_id=actor.id) as client:
        value = stored(actor, token)
        if value is not DROP:
            with client.session_transaction() as session:
                session[READINESS_CSRF_SESSION_KEY] = value
        response = client.post(
            f'{host}{path}', data=form(token), headers=ENGLISH
        )
        flashes = _flashes(client)
    return SimpleNamespace(
        status=response.status_code,
        location=response.location,
        flashes=flashes,
        token=token,
    )


def route_path(world, route):
    return f'/matches/{world.match_id}/ready/{route}'


_FORM = re.compile(r'<form\b[^>]*\baction="([^"]*)"[^>]*>(.*?)</form>', re.S | re.I)
_INPUT = re.compile(r'<input\b[^>]*>', re.I)
_NAME = re.compile(r'\sname="([^"]*)"')
_VALUE = re.compile(r'\svalue="([^"]*)"')


def rendered_form_targets(page, suffix):
    """(action, fields) of every rendered form posting to `suffix`."""
    forms = []
    for raw_action, body in _FORM.findall(page):
        action = htmllib.unescape(raw_action)
        if not action.endswith(suffix):
            continue
        fields = {}
        for tag in _INPUT.findall(body):
            name = _NAME.search(tag)
            if name is None:
                continue
            value = _VALUE.search(tag)
            fields[htmllib.unescape(name.group(1))] = (
                htmllib.unescape(value.group(1)) if value else ''
            )
        forms.append((action, fields))
    return forms


def rendered_forms(page, suffix):
    """The hidden and text fields of every rendered form posting to `suffix`."""
    return [fields for _, fields in rendered_form_targets(page, suffix)]


# -- persisted facts, read through a connection of their own --


def snapshot(world):
    """Everything a readiness request could write for this world."""
    with Session(db.engine) as session:
        match = session.get(DbTournamentMatch, world.match_id)
        row = (
            match.pairing_id,
            match.pairing_generation,
            match.readiness_revision,
            match.ready_at_a,
            match.ready_by_a,
            match.ready_at_b,
            match.ready_by_b,
            match.invitation_hold_a,
            match.invitation_hold_b,
            match.confirmed_by,
        )
        work = tuple(
            tuple(r)
            for r in session.execute(
                select(
                    DbMatchInvitation.id,
                    DbMatchInvitation.status,
                    DbMatchInvitation.attempts,
                    DbMatchInvitation.dispatch_token,
                    DbMatchInvitation.last_error,
                    DbMatchInvitation.expected_readiness_revision,
                )
                .where(DbMatchInvitation.match_id == world.match_id)
                .order_by(DbMatchInvitation.id)
            )
        )
        log = tuple(
            session.scalars(
                select(DbTournamentLogEntry.id)
                .where(DbTournamentLogEntry.tournament_id == world.tournament_id)
                .order_by(DbTournamentLogEntry.id)
            )
        )
        pairings = tuple(
            session.scalars(
                select(DbMatchPairing.id)
                .where(DbMatchPairing.match_id == world.match_id)
                .order_by(DbMatchPairing.id)
            )
        )
        status = session.get(DbTournament, world.tournament_id).tournament_status
    return row, work, log, pairings, status


def revisions(world):
    with Session(db.engine) as session:
        match = session.get(DbTournamentMatch, world.match_id)
        return match.pairing_generation, match.readiness_revision


def audit(world):
    """(type, initiator, data) of the readiness audit entries, oldest first."""
    with Session(db.engine) as session:
        entries = session.scalars(
            select(DbTournamentLogEntry)
            .where(DbTournamentLogEntry.tournament_id == world.tournament_id)
            .order_by(DbTournamentLogEntry.occurred_at, DbTournamentLogEntry.id)
        ).all()
        return [
            (e.event_type, e.initiator_id, e.data)
            for e in entries
            if e.event_type.startswith('match-ready-')
        ]


def ready_now(world, side):
    """Make a side ready by the real operation, committed, before the request."""
    generation, revision = revisions(world)
    result = REAL_CLAIM(
        world.match_id,
        side,
        world.actors[side].id,
        expected_pairing_generation=generation,
        expected_readiness_revision=revision,
    )
    assert result.is_ok(), result
    repo.commit_session()


# -- fixtures --


@pytest.fixture(scope='module')
def foreign_party(make_party, brand):
    return make_party(
        brand, PartyID('readiness-http-foreign'), 'Readiness HTTP Foreign'
    )


@pytest.fixture(scope='module')
def cast(make_user, make_admin):
    def logged_in(user):
        log_in_user(user.id)
        return user

    return SimpleNamespace(
        players=[
            logged_in(make_user(f'Http23Player{i}')) for i in range(3)
        ],
        outsider=logged_in(make_user('Http23Outsider')),
        orga=logged_in(make_user('Http23Orga')),
        sibling_orga=logged_in(make_user('Http23SiblingOrga')),
        lookalike=logged_in(
            make_admin(
                {
                    'lan_tournament.view',
                    'lan_tournament.update',
                    'lan_tournament.maintain',
                }
            )
        ),
        viewer=logged_in(make_admin({'admin.access', 'lan_tournament.view'})),
        admin=logged_in(
            make_admin(
                {
                    'admin.access',
                    'lan_tournament.administrate',
                    'lan_tournament.view',
                }
            )
        ),
    )


def _purge(tournament_id):
    db.session.rollback()
    if repo.find_tournament(tournament_id) is not None:
        lifecycle.delete_tournament(tournament_id)
    for model in (DbMatchInvitation, DbMatchPairing, DbTournamentLogEntry):
        db.session.execute(
            delete(model).where(model.tournament_id == tournament_id)
        )
    db.session.commit()


@pytest.fixture
def build_world(admin_app, cast):
    """Create ONGOING 1v1 worlds: two players in one match, pairing set."""
    created = []

    def build(party_id, *, status='ONGOING'):
        now = datetime.now(UTC).replace(tzinfo=None)
        tournament = DbTournament(
            uuid7(),
            party_id,
            f'Readiness HTTP {uuid7()}',
            now,
            game_format='ONE_V_ONE',
            elimination_mode='SINGLE_ELIMINATION',
            tournament_status=status,
        )
        tournament_id = tournament.id
        created.append(tournament_id)
        db.session.add(tournament)
        db.session.flush()
        participants = [
            DbTournamentParticipant(uuid7(), user.id, tournament_id, now)
            for user in cast.players
        ]
        db.session.add_all(participants)
        match = DbTournamentMatch(
            uuid7(), tournament_id, now, match_order=0, round=0
        )
        match_id = match.id
        db.session.add(match)
        db.session.flush()
        for participant in participants[:2]:
            db.session.add(
                DbTournamentMatchToContestant(
                    uuid7(), match_id, now, participant_id=participant.id
                )
            )
        db.session.commit()
        user_of = {
            p.id: user
            for p, user in zip(participants, cast.players, strict=True)
        }
        world = SimpleNamespace(
            tournament_id=tournament_id,
            match_id=match_id,
            participant_ids=[p.id for p in participants],
            user_of=user_of,
        )
        refreshed = readiness.refresh_pairing_and_invitations_flush(
            match_id, occurred_at=now
        )
        assert refreshed.is_ok(), refreshed
        db.session.commit()
        pairing = repo.get_match_pairing(match_id)
        db.session.rollback()
        world.actors = {
            A: user_of[pairing.side_a.id],
            B: user_of[pairing.side_b.id],
        }
        return world

    yield build
    db.session.rollback()
    for tournament_id in created:
        _purge(tournament_id)


@pytest.fixture
def spy(monkeypatch):
    """Record whether the service ran and which effects fired afterwards."""
    state = SimpleNamespace(claims=0, revokes=0, dispatched=[], events=[])

    def claim_flush(*args, **kwargs):
        state.claims += 1
        return REAL_CLAIM(*args, **kwargs)

    def revoke_flush(*args, **kwargs):
        state.revokes += 1
        return REAL_REVOKE(*args, **kwargs)

    def enqueue(function, *args, **kwargs):
        # Post-commit dispatch is one queue job per batch; record the hand-over.
        assert function is invitations.dispatch_match_invitations
        state.dispatched.append(tuple(args[0]))

    def receiver(sender, *, event):
        state.events.append(event)

    def reset():
        state.claims = state.revokes = 0
        state.dispatched.clear()
        state.events.clear()

    state.reset = reset
    monkeypatch.setattr(readiness, 'claim_ready_flush', claim_flush)
    monkeypatch.setattr(readiness, 'revoke_ready_flush', revoke_flush)
    monkeypatch.setattr(invitations.jobqueue, 'enqueue', enqueue)
    connected = (
        signals.match_ready_claimed,
        signals.match_ready_revoked,
        signals.match_both_ready,
    )
    for signal in connected:
        signal.connect(receiver, weak=False)
    try:
        yield state
    finally:
        for signal in connected:
            signal.disconnect(receiver)


def assert_nothing_written(world, before, spy, *, service_reached=False):
    assert snapshot(world) == before
    if not service_reached:
        assert spy.claims == 0 and spy.revokes == 0
    assert spy.events == []
    assert spy.dispatched == []


# -- the rendered controls and their tokens --


def test_rendered_forms_round_trip_and_tokens_are_user_bound(
    site_app, build_world, party, cast, spy
):
    world = build_world(party.id)
    alice, bob = world.actors[A], world.actors[B]
    path = f'/matches/{world.match_id}'
    claim_url = SITE_ROOT + route_path(world, 'claim')
    with (
        http_client(site_app, user_id=alice.id) as alice_client,
        http_client(site_app, user_id=bob.id) as bob_client,
    ):
        alice_page = alice_client.get(f'{SITE_ROOT}{path}', headers=ENGLISH)
        bob_page = bob_client.get(f'{SITE_ROOT}{path}', headers=ENGLISH)
        assert alice_page.status_code == bob_page.status_code == 200
        (alice_fields,) = rendered_forms(
            alice_page.get_data(as_text=True), '/ready/claim'
        )
        (bob_fields,) = rendered_forms(
            bob_page.get_data(as_text=True), '/ready/claim'
        )
        # Each player is offered the own side only, with the own token.
        assert alice_fields['side'] == 'a' and bob_fields['side'] == 'b'
        assert len(alice_fields['csrf_token']) == 43
        assert alice_fields['csrf_token'] != bob_fields['csrf_token']
        generation, revision = revisions(world)
        assert alice_fields['expected_pairing_generation'] == str(generation)
        assert alice_fields['expected_readiness_revision'] == str(revision)
        before = snapshot(world)

        # Alice's rendered form, posted in Bob's session: foreign-user token.
        foreign = bob_client.post(
            claim_url,
            data=alice_fields,
            headers=ENGLISH,
        )
        assert foreign.status_code == 403
        assert_nothing_written(world, before, spy)

        # The same form in her own session claims exactly once.
        own = alice_client.post(
            claim_url,
            data=alice_fields,
            headers=ENGLISH,
        )
        assert own.status_code == 302
        assert own.location.endswith(path)
        assert 'Readiness claimed.' in _flashes(alice_client)
        types = [entry[0] for entry in audit(world)]
        assert types == ['match-ready-claimed']
        after_claim = snapshot(world)
        assert after_claim != before
        assert len(spy.events) == 1 and spy.events[0].claimed_by == alice.id
        assert len(spy.dispatched) == 1
        spy.reset()

        # Replaying that very form is a stale revision and writes nothing.
        replay = alice_client.post(
            claim_url,
            data=alice_fields,
            headers=ENGLISH,
        )
        assert replay.status_code == 302
        assert 'readiness_conflict' in _flashes(alice_client)
        assert_nothing_written(world, after_claim, spy, service_reached=True)

        # The page now offers the un-ready control, no claim. It posts the
        # very fields of the claim form and nothing else.
        again = alice_client.get(f'{SITE_ROOT}{path}', headers=ENGLISH)
        page = again.get_data(as_text=True)
        assert rendered_forms(page, '/ready/claim') == []
        (revoke_fields,) = rendered_forms(page, '/ready/revoke')
        assert revoke_fields['side'] == 'a'
        assert set(revoke_fields) == set(alice_fields)
        assert revoke_fields['csrf_token'] == alice_fields['csrf_token']


def test_unready_round_trip_through_rendered_form(
    site_app, build_world, party, spy
):
    world = build_world(party.id)
    alice = world.actors[A]
    ready_now(world, A)
    path = f'/matches/{world.match_id}'
    with http_client(site_app, user_id=alice.id) as client:
        page = client.get(f'{SITE_ROOT}{path}', headers=ENGLISH)
        assert page.status_code == 200
        html = page.get_data(as_text=True)
        assert rendered_forms(html, '/ready/claim') == []
        # The form exactly as rendered: its action and every input, no
        # hand-seeded session token and no field added by the test.
        ((action, fields),) = rendered_form_targets(html, '/ready/revoke')
        assert fields['side'] == 'a' and len(fields['csrf_token']) == 43
        before = snapshot(world)
        row, *_ = before
        assert row[3] is not None and row[4] == alice.id  # side A is ready
        spy.reset()

        reply = client.post(
            urljoin(f'{SITE_HOST}/', action), data=fields, headers=ENGLISH
        )
        assert reply.status_code == 302 and reply.location.endswith(path)
        assert 'Readiness revoked.' in _flashes(client)

        after = snapshot(world)
        row, *_ = after
        assert row[3] is None and row[4] is None  # ready_at_a, ready_by_a
        assert row[5] is None and row[6] is None  # side B untouched
        assert row[2] == before[0][2] + 1  # readiness_revision
        assert (spy.claims, spy.revokes) == (0, 1)
        (event,) = spy.events
        assert event.revoked_by == alice.id and event.side is A
        entries = audit(world)
        assert [entry[0] for entry in entries] == [
            'match-ready-claimed',
            'match-ready-revoked',
        ]
        _, initiator, data = entries[-1]
        assert initiator == alice.id
        assert data['actor_role'] == 'player' and data['side'] == 'a'
        assert 'revoked_at' in data

        # The page flips back to the claim control for the same side.
        again = client.get(f'{SITE_ROOT}{path}', headers=ENGLISH)
        html = again.get_data(as_text=True)
        assert rendered_forms(html, '/ready/revoke') == []
        (claim_fields,) = rendered_forms(html, '/ready/claim')
        assert claim_fields['side'] == 'a'

        # Posting the old form again is a stale revision and writes nothing.
        spy.reset()
        replay = client.post(
            urljoin(f'{SITE_HOST}/', action), data=fields, headers=ENGLISH
        )
        assert replay.status_code == 302
        assert 'readiness_conflict' in _flashes(client)
        assert_nothing_written(world, after, spy, service_reached=True)


# -- authentication and CSRF --


@pytest.mark.parametrize('route', ['claim', 'revoke'])
def test_anonymous_post_is_redirected_without_a_write(
    site_app, build_world, party, spy, route
):
    world = build_world(party.id)
    if route == 'revoke':
        ready_now(world, A)
    before = snapshot(world)
    spy.reset()
    token = secrets.token_urlsafe(32)
    client = site_app.test_client()
    with client.session_transaction() as session:
        session[READINESS_CSRF_SESSION_KEY] = {
            'user_id': str(world.actors[A].id),
            'token': token,
        }
    response = client.post(
        f'{SITE_ROOT}{route_path(world, route)}',
        data=make_form(token, world, A, route=route),
        headers=ENGLISH,
    )
    assert response.status_code == 302
    assert 'log_in' in response.location
    assert_nothing_written(world, before, spy)


# fmt: off
CSRF_CASES = {
    'field-missing': (armed, lambda token: {'csrf_token': DROP}),
    'field-empty': (armed, lambda token: {'csrf_token': ''}),
    'field-wrong': (
        armed, lambda token: {'csrf_token': secrets.token_urlsafe(32)}),
    'field-one-short': (
        armed, lambda token: {'csrf_token': token[:-1]}),
    'field-one-long': (
        armed, lambda token: {'csrf_token': token + 'A'}),
    'field-bad-characters': (
        armed, lambda token: {'csrf_token': '!' * 43}),
    'field-repeated': (
        armed, lambda token: {'csrf_token': [token, token]}),
    'field-repeated-with-wrong': (
        armed, lambda token: {'csrf_token': [token, 'x' * 43]}),
    'session-has-no-token': (nothing, lambda token: {}),
    'session-token-of-another-user': (
        lambda actor, token: {'user_id': str(uuid7()), 'token': token},
        lambda token: {}),
    'session-token-without-user': (
        lambda actor, token: {'token': token}, lambda token: {}),
    'session-token-is-a-bare-string': (
        lambda actor, token: token, lambda token: {}),
    'session-token-malformed': (
        lambda actor, token: {'user_id': str(actor.id), 'token': 'short'},
        lambda token: {'csrf_token': 'short'}),
}
# fmt: on


@pytest.mark.parametrize('route', ['claim', 'revoke'])
@pytest.mark.parametrize('case', sorted(CSRF_CASES))
def test_missing_wrong_and_foreign_csrf_tokens_are_refused(
    site_app, build_world, party, spy, route, case
):
    stored, override = CSRF_CASES[case]
    world = build_world(party.id)
    if route == 'revoke':
        ready_now(world, A)
    actor = world.actors[A]
    before = snapshot(world)
    spy.reset()
    reply = post(
        site_app,
        actor,
        route_path(world, route),
        lambda token: make_form(
            token, world, A, route=route, **override(token)
        ),
        stored=stored,
    )
    # The token is checked before any lookup: not even the service is reached.
    assert reply.status == 403
    assert_nothing_written(world, before, spy)


# -- forged side and orga fields --


@pytest.mark.parametrize('route', ['claim', 'revoke'])
@pytest.mark.parametrize(
    'who',
    [
        'player-for-the-opponent-side',
        'outsider',
        'sibling-tournament-orga',
        'lookalike-permissions',
        'view-only-permission',
    ],
)
def test_forged_side_and_orga_fields_cannot_widen_authority(
    site_app, build_world, party, cast, spy, route, who
):
    world = build_world(party.id)
    sibling = build_world(party.id)
    orgas.assign_orga(
        sibling.tournament_id, cast.sibling_orga.id, cast.admin.id
    ).unwrap()
    if route == 'revoke':
        ready_now(world, A)
        ready_now(world, B)
    actor, side = {
        'player-for-the-opponent-side': (world.actors[A], B),
        'outsider': (cast.outsider, A),
        'sibling-tournament-orga': (cast.sibling_orga, A),
        'lookalike-permissions': (cast.lookalike, A),
        'view-only-permission': (cast.viewer, A),
    }[who]
    forged = dict(
        FORGED,
        user_id=str(world.actors[side].id),
        initiator_id=str(world.actors[side].id),
        claimed_by=str(world.actors[side].id),
    )
    before = snapshot(world)
    sibling_before = snapshot(sibling)
    spy.reset()
    reply = post(
        site_app,
        actor,
        route_path(world, route),
        lambda token: make_form(token, world, side, route=route, **forged),
    )
    assert reply.status == 403
    assert_nothing_written(world, before, spy, service_reached=True)
    assert snapshot(sibling) == sibling_before
    # The authority check ran under the lock and refused, exactly once.
    assert (spy.claims, spy.revokes) == (
        (1, 0) if route == 'claim' else (0, 1)
    )


def test_a_forged_actor_field_cannot_replace_the_logged_in_user(
    site_app, build_world, party, spy
):
    world = build_world(party.id)
    alice, bob = world.actors[A], world.actors[B]
    forged = dict(
        FORGED,
        user_id=str(bob.id),
        initiator_id=str(bob.id),
        claimed_by=str(bob.id),
    )
    reply = post(
        site_app,
        alice,
        route_path(world, 'claim'),
        lambda token: make_form(token, world, A, route='claim', **forged),
    )
    assert reply.status == 302 and 'Readiness claimed.' in reply.flashes
    ((kind, initiator, data),) = audit(world)
    assert kind == 'match-ready-claimed'
    assert initiator == alice.id
    assert data['actor_role'] == 'player' and data['side'] == 'a'
    row, *_ = snapshot(world)
    assert row[4] == alice.id and row[3] is not None  # ready_by_a, ready_at_a
    assert row[5] is None and row[6] is None  # side B untouched
    assert spy.events[0].actor_role == 'player'
    assert spy.events[0].claimed_by == alice.id


@pytest.mark.parametrize('side', [A, B], ids=['side-a', 'side-b'])
@pytest.mark.parametrize('who', ['scoped-orga', 'global-admin'])
def test_resolved_orga_authority_is_audited_as_orga_not_as_posted(
    site_app, build_world, party, cast, spy, who, side
):
    world = build_world(party.id)
    orgas.assign_orga(
        world.tournament_id, cast.orga.id, cast.admin.id
    ).unwrap()
    actor = cast.orga if who == 'scoped-orga' else cast.admin
    reply = post(
        site_app,
        actor,
        route_path(world, 'claim'),
        # No orga flag is posted at all; the server resolves the authority.
        lambda token: make_form(token, world, side, route='claim'),
    )
    assert reply.status == 302 and 'Readiness claimed.' in reply.flashes
    ((kind, initiator, data),) = audit(world)
    assert kind == 'match-ready-claimed' and initiator == actor.id
    assert data['actor_role'] == 'orga' and data['side'] == side.value
    row, *_ = snapshot(world)
    ready_by = row[4] if side is A else row[6]
    other_by = row[6] if side is A else row[4]
    assert ready_by == actor.id and other_by is None


# -- bounded fields --


# fmt: off
FIELD_CASES = {
    'side-empty': {'side': ''},
    'side-unknown': {'side': 'c'},
    'side-upper-case': {'side': 'A'},
    'side-two-letters': {'side': 'ab'},
    'side-padded': {'side': 'a '},
    'side-missing': {'side': DROP},
    'side-repeated': {'side': ['a', 'b']},
    'generation-empty': {'expected_pairing_generation': ''},
    'generation-negative': {'expected_pairing_generation': '-1'},
    'generation-fraction': {'expected_pairing_generation': '1.5'},
    'generation-boolean': {'expected_pairing_generation': 'true'},
    'generation-exponent': {'expected_pairing_generation': '1e3'},
    'generation-hex': {'expected_pairing_generation': '0x10'},
    'generation-padded': {'expected_pairing_generation': ' 1'},
    'generation-unicode-digit': {'expected_pairing_generation': '\u0663'},
    'generation-over-bigint': {
        'expected_pairing_generation': '9223372036854775808'},
    'generation-missing': {'expected_pairing_generation': DROP},
    'generation-repeated': {'expected_pairing_generation': ['1', '1']},
    'revision-empty': {'expected_readiness_revision': ''},
    'revision-negative': {'expected_readiness_revision': '-1'},
    'revision-boolean': {'expected_readiness_revision': 'false'},
    'revision-twenty-digits': {
        'expected_readiness_revision': '1' * 20},
    'revision-missing': {'expected_readiness_revision': DROP},
    'revision-repeated': {'expected_readiness_revision': ['1', '1']},
}
# fmt: on


@pytest.mark.parametrize('route', ['claim', 'revoke'])
@pytest.mark.parametrize('case', sorted(FIELD_CASES))
def test_malformed_fields_are_bounced_before_any_lookup(
    site_app, build_world, party, spy, route, case
):
    world = build_world(party.id)
    if route == 'revoke':
        ready_now(world, A)
    before = snapshot(world)
    spy.reset()
    reply = post(
        site_app,
        world.actors[A],
        route_path(world, route),
        lambda token: make_form(
            token, world, A, route=route, **FIELD_CASES[case]
        ),
    )
    assert reply.status == 302
    assert 'Invalid form data.' in reply.flashes
    assert reply.location.endswith(f'/matches/{world.match_id}')
    assert_nothing_written(world, before, spy)


# -- party scope and unknown subjects --


@pytest.mark.parametrize('route', ['claim', 'revoke'])
@pytest.mark.parametrize(
    'who', ['participant', 'scoped-orga', 'global-admin']
)
def test_cross_party_match_is_refused_even_with_every_authority(
    site_app, build_world, party, foreign_party, cast, spy, route, who
):
    foreign = build_world(foreign_party.id)
    orgas.assign_orga(
        foreign.tournament_id, cast.orga.id, cast.admin.id
    ).unwrap()
    if route == 'revoke':
        ready_now(foreign, A)
    actor = {
        'participant': foreign.actors[A],
        'scoped-orga': cast.orga,
        'global-admin': cast.admin,
    }[who]
    before = snapshot(foreign)
    spy.reset()
    reply = post(
        site_app,
        actor,
        route_path(foreign, route),
        lambda token: make_form(token, foreign, A, route=route),
    )
    # The current site belongs to another party: the match does not exist here.
    assert reply.status == 404
    assert_nothing_written(foreign, before, spy)


@pytest.mark.parametrize('route', ['claim', 'revoke'])
@pytest.mark.parametrize(
    'match_id',
    [
        'not-a-uuid',
        '00000000-0000-0000-0000-000000000000',
        "' OR '1'='1",
        '%00',
    ],
    ids=['malformed', 'unknown', 'sql-like', 'percent-escape'],
)
def test_malformed_and_unknown_match_ids_are_not_found(
    site_app, build_world, party, spy, route, match_id
):
    world = build_world(party.id)
    quoted_id = quote(match_id, safe='')
    before = snapshot(world)
    spy.reset()
    reply = post(
        site_app,
        world.actors[A],
        f'/matches/{quoted_id}/ready/{route}',
        lambda token: make_form(token, world, A, route=route),
    )
    assert reply.status == 404
    assert_nothing_written(world, before, spy)


# -- stale generation or revision --


# fmt: off
STALE_CASES = {
    'revision-behind': lambda g, r: (g, r - 1),
    'revision-ahead': lambda g, r: (g, r + 1),
    'generation-behind': lambda g, r: (g - 1, r),
    'generation-ahead': lambda g, r: (g + 1, r),
    'both-ahead': lambda g, r: (g + 5, r + 5),
}
# fmt: on


@pytest.mark.parametrize('route', ['claim', 'revoke'])
@pytest.mark.parametrize('case', sorted(STALE_CASES))
def test_stale_generation_or_revision_is_refused_without_a_write(
    site_app, build_world, party, spy, route, case
):
    world = build_world(party.id)
    if route == 'revoke':
        ready_now(world, A)
    generation, revision = revisions(world)
    assert generation >= 1 and revision >= 1
    stale_generation, stale_revision = STALE_CASES[case](generation, revision)
    before = snapshot(world)
    spy.reset()
    reply = post(
        site_app,
        world.actors[A],
        route_path(world, route),
        lambda token: make_form(
            token,
            world,
            A,
            route=route,
            generation=stale_generation,
            revision=stale_revision,
        ),
    )
    assert reply.status == 302
    assert 'readiness_conflict' in reply.flashes
    # The service ran under the lock, refused, and the caller rolled back.
    assert_nothing_written(world, before, spy, service_reached=True)


def test_a_replaced_pairing_refuses_the_old_page_for_both_players(
    site_app, build_world, party, cast, spy
):
    world = build_world(party.id)
    old, newcomer = world.actors[A], cast.players[2]
    generation, revision = revisions(world)
    # The old occupant of side A is replaced through the sanctioned adapter.
    old_participant = next(p for p, u in world.user_of.items() if u == old)
    new_participant = next(
        p for p, u in world.user_of.items() if u == newcomer
    )
    repo.get_tournament_for_update(world.tournament_id)
    repo.lock_matches_for_update([world.match_id])
    engine._delete_contestant_from_match_flush(
        world.match_id, participant_id=old_participant
    )
    engine._create_match_contestant_flush(
        TournamentMatchToContestant(
            id=TournamentMatchToContestantID(uuid7()),
            tournament_match_id=world.match_id,
            team_id=None,
            participant_id=new_participant,
            score=None,
            created_at=datetime.now(UTC),
        )
    )
    repo.commit_session()
    db.session.rollback()
    before = snapshot(world)
    assert revisions(world)[0] > generation
    spy.reset()
    for actor in (old, newcomer):
        reply = post(
            site_app,
            actor,
            route_path(world, 'claim'),
            lambda token: make_form(
                token,
                world,
                A,
                route='claim',
                generation=generation,
                revision=revision,
            ),
        )
        assert reply.status == 302 and 'readiness_conflict' in reply.flashes
        assert snapshot(world) == before
    assert spy.events == [] and spy.dispatched == []
    # With fresh state the replaced player has no authority any more.
    fresh = post(
        site_app,
        old,
        route_path(world, 'claim'),
        lambda token: make_form(token, world, A, route='claim'),
    )
    assert fresh.status == 403
    assert snapshot(world) == before


# -- lifecycle --


@pytest.mark.parametrize('route', ['claim', 'revoke'])
@pytest.mark.parametrize('status', ['PAUSED', 'COMPLETED', 'CANCELLED'])
def test_a_tournament_that_is_not_ongoing_refuses_requests(
    site_app, build_world, party, spy, route, status
):
    world = build_world(party.id)
    if route == 'revoke':
        ready_now(world, A)
    with Session(db.engine) as session:
        session.execute(
            update(DbTournament)
            .where(DbTournament.id == world.tournament_id)
            .values(tournament_status=status)
        )
        session.commit()
    before = snapshot(world)
    spy.reset()
    reply = post(
        site_app,
        world.actors[A],
        route_path(world, route),
        lambda token: make_form(token, world, A, route=route),
    )
    assert reply.status == 302
    assert 'Tournament is not in progress.' in reply.flashes
    assert_nothing_written(world, before, spy)


def test_a_draft_tournament_is_hidden(site_app, build_world, party, spy):
    world = build_world(party.id, status='DRAFT')
    before = snapshot(world)
    spy.reset()
    reply = post(
        site_app,
        world.actors[A],
        route_path(world, 'claim'),
        lambda token: make_form(token, world, A, route='claim'),
    )
    assert reply.status == 404
    assert_nothing_written(world, before, spy)


def test_a_confirmed_match_refuses_requests(site_app, build_world, party, spy):
    world = build_world(party.id)
    winner = world.actors[A]
    with Session(db.engine) as session:
        session.execute(
            update(DbTournamentMatch)
            .where(DbTournamentMatch.id == world.match_id)
            .values(confirmed_by=winner.id)
        )
        session.commit()
    before = snapshot(world)
    spy.reset()
    reply = post(
        site_app,
        winner,
        route_path(world, 'claim'),
        lambda token: make_form(token, world, A, route='claim'),
    )
    assert reply.status == 302 and 'match_confirmed' in reply.flashes
    assert_nothing_written(world, before, spy, service_reached=True)


def test_cross_party_match_page_is_not_found_for_every_viewer(
    site_app, build_world, foreign_party, cast
):
    foreign = build_world(foreign_party.id)
    for viewer in (None, foreign.actors[A], cast.orga, cast.admin):
        if viewer is None:
            response = site_app.test_client().get(
                f'{SITE_ROOT}/matches/{foreign.match_id}', headers=ENGLISH
            )
        else:
            with http_client(site_app, user_id=viewer.id) as client:
                response = client.get(
                    f'{SITE_ROOT}/matches/{foreign.match_id}',
                    headers=ENGLISH,
                )
        assert response.status_code == 404
