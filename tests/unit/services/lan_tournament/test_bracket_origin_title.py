"""
tests.unit.services.lan_tournament.test_bracket_origin_title
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The public bracket JSON names where a playoff contestant qualified from.
"""

import json
from unittest.mock import patch

import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)

from .test_bracket_serialization import (
    _make_contestant,
    _make_match,
    _make_tournament,
)

from tests.helpers import generate_uuid


CATALOGUE = {
    'Group %(letter)s, place %(rank)s': 'Gruppe %(letter)s, Platz %(rank)s',
}


def _german(text, **variables):
    return CATALOGUE.get(text, text) % variables if variables else text


def _serialize(contestants, origin_labels=None):
    tournament = _make_tournament()
    match = _make_match()
    match_data = [
        {
            'match': match,
            'contestants': [
                _make_contestant(match_id=match.id, **c) for c in contestants
            ],
        }
    ]
    return helpers.serialize_bracket_json(
        tournament,
        match_data,
        {},
        {},
        {},
        {},
        origin_labels=origin_labels,
    )


def _contestants(payload):
    return payload['matches'][0]['contestants']


def _keys_of(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys_of(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys_of(inner)


# fmt: off
@pytest.mark.parametrize('label,title', [
    ('A1', 'Group A, place 1'),
    ('C2', 'Group C, place 2'),
    ('B10', 'Group B, place 10'),
    ('weird', None),
    ('', None),
])
# fmt: on
def test_the_title_spells_out_the_group_and_place(label, title):
    pid = TournamentParticipantID(generate_uuid())

    payload = _serialize([{'participant_id': pid}], {str(pid): label})

    assert _contestants(payload)[0]['origin_title'] == title


def test_only_a_contestant_with_an_origin_carries_a_title():
    with_origin = TournamentParticipantID(generate_uuid())
    without_origin = TournamentParticipantID(generate_uuid())

    payload = _serialize(
        [{'participant_id': with_origin}, {'participant_id': without_origin}],
        {str(with_origin): 'C2'},
    )

    first, second = _contestants(payload)
    assert (first['origin'], first['origin_title']) == (
        'C2',
        'Group C, place 2',
    )
    assert (second['origin'], second['origin_title']) == (None, None)


def test_no_origin_labels_mean_no_titles():
    pid = TournamentParticipantID(generate_uuid())

    payload = _serialize([{'participant_id': pid}])

    assert _contestants(payload)[0]['origin_title'] is None


def test_a_team_contestant_gets_its_title_by_team_id():
    team_id = TournamentTeamID(generate_uuid())

    payload = _serialize([{'team_id': team_id}], {str(team_id): 'D1'})

    assert _contestants(payload)[0]['origin_title'] == 'Group D, place 1'


def test_the_title_is_translated_server_side():
    pid = TournamentParticipantID(generate_uuid())

    with patch.object(helpers, 'gettext', side_effect=_german):
        payload = _serialize([{'participant_id': pid}], {str(pid): 'C2'})

    assert _contestants(payload)[0]['origin_title'] == 'Gruppe C, Platz 2'


def test_the_payload_stays_public():
    pid = TournamentParticipantID(generate_uuid())
    team_id = TournamentTeamID(generate_uuid())

    payload = _serialize(
        [{'participant_id': pid}, {'team_id': team_id}],
        {str(pid): 'A1', str(team_id): 'B2'},
    )

    for contestant in _contestants(payload):
        assert set(contestant) == {
            'name',
            'score',
            'team_id',
            'participant_id',
            'origin',
            'origin_title',
        }
    assert not {'seed', 'tier', 'code', 'reason'} & set(_keys_of(payload))
    assert json.loads(json.dumps(payload)) == payload
