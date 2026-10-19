"""
tests.unit.services.lan_tournament.test_homepage_cards
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime
from types import SimpleNamespace

from flask import Flask, g
import pytest

from byceps.services.lan_tournament.blueprints.site import homepage_cards
from byceps.services.lan_tournament.blueprints.site.homepage_cards import (
    build_homepage_cards,
    get_homepage_tournament_cards,
    HomepageTournamentCard,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID

from tests.helpers import generate_uuid


NOW = datetime(2026, 10, 19, 18, 0, 0)

ONGOING = TournamentStatus.ONGOING
PAUSED = TournamentStatus.PAUSED
OPEN = TournamentStatus.REGISTRATION_OPEN
CLOSED = TournamentStatus.REGISTRATION_CLOSED
COMPLETED = TournamentStatus.COMPLETED


def _make_tournament(**kwargs) -> Tournament:
    defaults = {
        'id': TournamentID(generate_uuid()),
        'party_id': PartyID('test-party'),
        'name': 'Test Tournament',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': ContestantType.SOLO,
        'tournament_status': OPEN,
        'game_format': None,
        'elimination_mode': None,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


@pytest.mark.parametrize(
    'status',
    [None, TournamentStatus.DRAFT, TournamentStatus.CANCELLED],
)
def test_build_homepage_cards_excludes_draft_cancelled_and_unset_status(
    status,
):
    hidden = _make_tournament(name='Hidden', tournament_status=status)
    shown = _make_tournament(name='Shown', tournament_status=OPEN)

    cards = build_homepage_cards([hidden, shown], {}, {})

    assert [c.name for c in cards] == ['Shown']


# fmt: off
@pytest.mark.parametrize(
    ('tournaments', 'expected'),
    [
        # Live, open, closed, completed, whatever the position says.
        (
            [(COMPLETED, 0), (CLOSED, 1), (OPEN, 2), (ONGOING, 3)],
            [3, 2, 1, 0],
        ),
        # Ongoing and paused rank as one live group, ordered by position.
        (
            [(ONGOING, 5), (PAUSED, 2), (OPEN, 0)],
            [1, 0, 2],
        ),
        # Within one status, the admin-defined position decides.
        (
            [(OPEN, 3), (OPEN, 1), (OPEN, 2)],
            [1, 2, 0],
        ),
        # Mixed statuses and positions.
        (
            [(COMPLETED, 1), (OPEN, 2), (OPEN, 1), (ONGOING, 9), (CLOSED, 0)],
            [3, 2, 1, 4, 0],
        ),
    ],
)
# fmt: on
def test_build_homepage_cards_orders_live_open_closed_completed_then_position(
    tournaments, expected
):
    built = [
        _make_tournament(name=f't{i}', tournament_status=status, position=pos)
        for i, (status, pos) in enumerate(tournaments)
    ]

    cards = build_homepage_cards(built, {}, {}, limit=len(built))

    assert [c.name for c in cards] == [f't{i}' for i in expected]


def test_build_homepage_cards_uses_team_count_for_team_tournaments():
    tournament = _make_tournament(
        name='Team cup',
        game='Rocket League',
        contestant_type=ContestantType.TEAM,
        tournament_status=ONGOING,
        start_time=NOW,
        max_players=64,
        max_teams=8,
    )
    participant_counts = {tournament.id: 24}
    team_counts = {tournament.id: 6}

    cards = build_homepage_cards([tournament], participant_counts, team_counts)

    assert cards == [
        HomepageTournamentCard(
            tournament_id=tournament.id,
            name='Team cup',
            game='Rocket League',
            status=ONGOING,
            is_team_based=True,
            filled=6,
            capacity=8,
            starts_at=NOW,
        )
    ]


def test_build_homepage_cards_derives_team_type_for_legacy_null_contestant_type():
    tournament = _make_tournament(
        contestant_type=None, max_players_in_team=5, max_teams=4
    )

    (card,) = build_homepage_cards(
        [tournament], {tournament.id: 20}, {tournament.id: 3}
    )

    assert card.is_team_based
    assert card.filled == 3
    assert card.capacity == 4


def test_build_homepage_cards_uses_participant_count_for_solo_tournaments():
    tournament = _make_tournament(
        name='Duel',
        game=None,
        contestant_type=ContestantType.SOLO,
        tournament_status=OPEN,
        max_players=16,
        max_teams=8,
    )
    participant_counts = {tournament.id: 11}
    team_counts = {tournament.id: 3}

    cards = build_homepage_cards([tournament], participant_counts, team_counts)

    assert cards == [
        HomepageTournamentCard(
            tournament_id=tournament.id,
            name='Duel',
            game=None,
            status=OPEN,
            is_team_based=False,
            filled=11,
            capacity=16,
            starts_at=None,
        )
    ]


def test_build_homepage_cards_counts_default_to_zero_and_capacity_to_none():
    tournament = _make_tournament()

    (card,) = build_homepage_cards([tournament], {}, {})

    assert card.filled == 0
    assert card.capacity is None


@pytest.mark.parametrize(
    ('limit', 'expected_count'),
    [(None, 4), (2, 2), (6, 6), (9, 6), (0, 0)],
)
def test_build_homepage_cards_respects_limit(limit, expected_count):
    tournaments = [
        _make_tournament(name=f't{i}', position=i) for i in range(6)
    ]
    kwargs = {} if limit is None else {'limit': limit}

    cards = build_homepage_cards(tournaments, {}, {}, **kwargs)

    assert [c.name for c in cards] == [f't{i}' for i in range(expected_count)]


def test_build_homepage_cards_applies_limit_after_ordering():
    tournaments = [
        _make_tournament(name='done', tournament_status=COMPLETED, position=0),
        _make_tournament(name='live', tournament_status=ONGOING, position=9),
    ]

    cards = build_homepage_cards(tournaments, {}, {}, limit=1)

    assert [c.name for c in cards] == ['live']


@pytest.mark.parametrize('party', [None, 'unset'])
def test_get_homepage_tournament_cards_without_party_returns_empty(
    monkeypatch, party
):
    def fail(*args, **kwargs):
        raise AssertionError('services must not be queried without a party')

    monkeypatch.setattr(
        homepage_cards.tournament_service, 'get_tournaments_for_party', fail
    )

    with Flask(__name__).app_context():
        if party != 'unset':
            g.party = party

        assert get_homepage_tournament_cards() == []


def test_get_homepage_tournament_cards_reads_counts_for_the_current_party(
    monkeypatch,
):
    solo = _make_tournament(name='Solo', position=2, max_players=8)
    team = _make_tournament(
        name='Team',
        position=1,
        contestant_type=ContestantType.TEAM,
        max_teams=4,
    )
    draft = _make_tournament(
        name='Draft', position=0, tournament_status=TournamentStatus.DRAFT
    )
    queried = {}

    def get_tournaments_for_party(party_id):
        queried['party_id'] = party_id
        return [solo, team, draft]

    def get_participant_counts(tournament_ids):
        queried['participant_ids'] = list(tournament_ids)
        return {solo.id: 5, team.id: 99}

    def get_team_counts(tournament_ids):
        queried['team_ids'] = list(tournament_ids)
        return {solo.id: 99, team.id: 2}

    monkeypatch.setattr(
        homepage_cards.tournament_service,
        'get_tournaments_for_party',
        get_tournaments_for_party,
    )
    monkeypatch.setattr(
        homepage_cards.tournament_service,
        'get_participant_counts_for_tournaments',
        get_participant_counts,
    )
    monkeypatch.setattr(
        homepage_cards.tournament_team_service,
        'get_team_counts_for_tournaments',
        get_team_counts,
    )

    with Flask(__name__).app_context():
        g.party = SimpleNamespace(id=PartyID('lan-party'))

        cards = get_homepage_tournament_cards(limit=2)

    assert queried == {
        'party_id': 'lan-party',
        'participant_ids': [solo.id, team.id, draft.id],
        'team_ids': [solo.id, team.id, draft.id],
    }
    assert [(c.name, c.filled, c.capacity) for c in cards] == [
        ('Team', 2, 4),
        ('Solo', 5, 8),
    ]
