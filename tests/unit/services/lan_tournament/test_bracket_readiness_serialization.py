"""Public serialization consumes the shared projection, never raw claim fields."""

from dataclasses import replace
import json
from unittest.mock import patch

import pytest

from byceps.services.lan_tournament import lan_tournament_view_helpers as helpers
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus

from .test_readiness_surface_policy import NOW, _project, _row, _tournament


def _serialize(tournament, rows, **kwargs):
    with patch.object(helpers, 'gettext', side_effect=lambda text: f'de:{text}'):
        return helpers.serialize_bracket_json(
            tournament,
            [{'match': match, 'contestants': contestants}
             for match, contestants, _ in rows],
            {}, {}, {}, {}, **kwargs,
        )


# fmt: off
@pytest.mark.parametrize('count,claims,status,sides,label', [
    (0, 0, 'not_yet_occupied', [], 'Waiting for opponent'),
    (1, 0, 'not_yet_occupied', [], 'Waiting for opponent'),
    (2, 0, 'open', [], 'Not ready'),
    (2, 1, 'partially_ready', ['a'], 'Side A ready'),
    (2, 2, 'both_ready', ['a', 'b'], 'Both ready'),
])
# fmt: on
def test_public_canonical_facts_and_translated_labels(count, claims, status, sides, label):
    tournament = _tournament()
    rows = [_row(tournament, count=count, claims=claims)]
    projections = _project(tournament, rows)
    payload = _serialize(tournament, rows, readiness_by_match_id=projections)
    public = payload['matches'][0]['readiness']
    assert public == {
        'supported': True,
        'status': status,
        'label': f'de:{label}',
        'ready_sides': sides,
        'side_labels': {side: f'de:Side {side.upper()} ready' for side in sides},
        'assignment_complete': count == 2,
        'assigned_contestant_count': count,
        'original_occupied_since': rows[0][0].occupied_since.isoformat(),
        'pairing_started_at': NOW.isoformat() if count == 2 else None,
        'ready_at_a': NOW.isoformat() if claims else None,
        'ready_at_b': NOW.isoformat() if claims == 2 else None,
    }
    json.dumps(payload)
    if count == 2 and claims == 0:
        assert public['status'] == 'open'
        assert public['label'] == 'de:Not ready'
        assert public['ready_sides'] == []
        assert 'Ready' not in public['label']


def test_side_b_claim_is_labeled_by_stable_side_not_entrant_position():
    tournament = _tournament()
    match, contestants, pairing = _row(tournament, claims=1)
    match = replace(match, ready_at_a=None, ready_by_a=None,
                    ready_at_b=NOW, ready_by_b=match.ready_by_a)
    rows = [(match, list(reversed(contestants)), pairing)]
    public = _serialize(tournament, rows, readiness_by_match_id=_project(tournament, rows))['matches'][0]['readiness']
    assert public['ready_sides'] == ['b']
    assert public['label'] == 'de:Side B ready'
    assert public['ready_at_a'] is None
    assert public['ready_at_b'] == NOW.isoformat()


# fmt: off
@pytest.mark.parametrize('terminal,confirmed,count,status', [
    (None, True, 2, 'confirmed'),
    (None, True, 1, 'defwin'),
    (TournamentStatus.COMPLETED, False, 2, 'completed'),
    (TournamentStatus.CANCELLED, False, 2, 'cancelled'),
])
# fmt: on
def test_canonical_outcome_precedes_readiness(terminal, confirmed, count, status):
    tournament = _tournament(tournament_status=terminal or TournamentStatus.ONGOING)
    match, contestants, pairing = _row(tournament, count=count, claims=2)
    rows = [(replace(match, confirmed_by=match.tournament_id if confirmed else None), contestants, pairing)]
    public = _serialize(tournament, rows, readiness_by_match_id=_project(tournament, rows))['matches'][0]['readiness']
    assert public['status'] == status
    assert public['label'] == f'de:{status.capitalize() if status != "defwin" else "DEFWIN"}'


def test_unsupported_ffa_has_no_claim_facts_or_side_labels():
    tournament = _tournament(game_format=GameFormat.FREE_FOR_ALL)
    rows = [_row(tournament, claims=2)]
    public = _serialize(tournament, rows, readiness_by_match_id=_project(tournament, rows))['matches'][0]['readiness']
    assert public['supported'] is False
    assert public['ready_sides'] == []
    assert public['side_labels'] == {}
    assert public['ready_at_a'] is public['ready_at_b'] is None


def test_omitted_empty_and_partial_mapping_preserve_legacy_payload():
    tournament = _tournament()
    rows = [_row(tournament, claims=2), _row(tournament)]
    legacy = _serialize(tournament, rows)
    assert legacy == _serialize(tournament, rows, readiness_by_match_id=None)
    assert legacy == _serialize(tournament, rows, readiness_by_match_id={})
    projection = _project(tournament, rows)
    enriched = _serialize(tournament, rows, readiness_by_match_id={rows[0][0].id: projection[rows[0][0].id]})
    assert 'readiness' not in enriched['matches'][1]
    enriched['matches'][0].pop('readiness')
    assert enriched == legacy


def test_public_allowlist_excludes_private_actor_pairing_history_and_audience():
    tournament = _tournament()
    rows = [_row(tournament, claims=2)]
    projection = _project(tournament, rows)
    public = _serialize(tournament, rows, readiness_by_match_id=projection)['matches'][0]['readiness']
    assert set(public) == {
        'supported', 'status', 'label', 'ready_sides', 'side_labels',
        'assignment_complete', 'assigned_contestant_count',
        'original_occupied_since', 'pairing_started_at', 'ready_at_a', 'ready_at_b',
    }
    serialized = json.dumps(public)
    match = rows[0][0]
    for private_id in (match.ready_by_a, match.ready_by_b, match.pairing_id):
        assert str(private_id) not in serialized


def test_stale_raw_claims_are_not_rederived_by_serializer():
    tournament = _tournament()
    match, contestants, pairing = _row(tournament, claims=2)
    rows = [(replace(match, pairing_generation=99), contestants, pairing)]
    public = _serialize(tournament, rows, readiness_by_match_id=_project(tournament, rows))['matches'][0]['readiness']
    assert public['status'] == 'open'
    assert public['ready_sides'] == []
    assert public['ready_at_a'] is public['ready_at_b'] is None
