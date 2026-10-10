"""
tests.unit.services.lan_tournament.test_playoff_settings_validation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
from typing import Any
from unittest.mock import patch

import pytest

from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_service,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import (
    ScoreOrdering,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.validation_message import (
    ValidationMessage,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    TournamentSettings,
    validate_tournament_settings,
)
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


SE = EliminationMode.SINGLE_ELIMINATION
DE = EliminationMode.DOUBLE_ELIMINATION
RR = EliminationMode.ROUND_ROBIN
ONE = GameFormat.ONE_V_ONE
FFA = GameFormat.FREE_FOR_ALL
HS = GameFormat.HIGHSCORE

GROUPS_TOO_MANY = (
    'The minimum number of contestants is too small for this many groups.'
)
PLAYOFFS_TOO_FEW = 'Playoffs need at least 2 qualifiers in total.'
_REPO = (
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
_SIGNALS = 'byceps.services.lan_tournament.tournament_service.signals'


def _rr_settings(**overrides) -> TournamentSettings:
    fields: dict[str, Any] = {
        'contestant_type': ContestantType.SOLO,
        'game_format': ONE,
        'elimination_mode': RR,
        'score_ordering': None,
        'min_players': 8,
        'max_players': 16,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'point_table': None,
        'group_size_min': None,
        'group_size_max': None,
        'advancement_count': None,
        'playoff_game_format': ONE,
        'playoff_elimination_mode': SE,
        'playoff_group_count': 2,
        'playoff_qualifiers_per_group': 2,
        'playoff_release_mode': PlayoffReleaseMode.MANUAL,
    }
    fields.update(overrides)
    return TournamentSettings(**fields)


def _hs_settings(**overrides) -> TournamentSettings:
    fields: dict[str, Any] = {
        'game_format': HS,
        'elimination_mode': EliminationMode.NONE,
        'score_ordering': ScoreOrdering.HIGHER_IS_BETTER,
        'min_players': 4,
        'max_players': 32,
        'point_table': [3, 2, 1],
        'group_size_min': 2,
        'group_size_max': 4,
        'advancement_count': 1,
        'playoff_game_format': FFA,
        'playoff_elimination_mode': SE,
        'playoff_group_count': None,
        'playoff_qualifiers_per_group': None,
        'playoff_qualifier_count': 8,
        'playoff_release_mode': PlayoffReleaseMode.AUTOMATIC,
    }
    fields.update(overrides)
    return _rr_settings(**fields)


def _msgids(settings: TournamentSettings) -> dict[str, str]:
    result = validate_tournament_settings(settings, require_structure=True)
    if result.is_ok():
        return {}
    return {
        field: message.msgid for field, message in result.unwrap_err().items()
    }


def test_valid_configurations_pass():
    assert _msgids(_rr_settings()) == {}
    assert _msgids(_hs_settings()) == {}


def test_no_playoff_fields_pass_anywhere():
    assert (
        _msgids(
            _rr_settings(
                **{
                    'playoff_game_format': None,
                    'playoff_elimination_mode': None,
                    'playoff_group_count': None,
                    'playoff_qualifiers_per_group': None,
                    'playoff_release_mode': None,
                }
            )
        )
        == {}
    )


def test_group_count_above_the_code_limit_is_refused():
    open_ended = {'min_players': None, 'max_players': None}

    refused = validate_tournament_settings(
        _rr_settings(**open_ended, playoff_group_count=256),
        require_structure=True,
    )

    message = refused.unwrap_err()['playoff_group_count']
    assert message.msgid == 'At most %(max)s.'
    assert dict(message.params) == {'max': 255}
    assert _msgids(_rr_settings(**open_ended, playoff_group_count=255)) == {}


def test_more_groups_than_the_maximum_allows_is_refused():
    capped = {'min_players': None, 'max_players': 6}

    assert _msgids(_rr_settings(**capped, playoff_group_count=4)) == {
        'playoff_group_count': (
            'The maximum number of contestants is too small for this many '
            'groups.'
        )
    }
    assert _msgids(_rr_settings(**capped, playoff_group_count=3)) == {}


# fmt: off
@pytest.mark.parametrize(
    ('overrides', 'field', 'msgid'),
    [
        ({'playoff_group_count': None}, 'playoff_group_count',
         'Please enter the number of groups.'),
        ({'playoff_group_count': 0}, 'playoff_group_count',
         'At least one group is needed.'),
        ({'playoff_group_count': -1}, 'playoff_group_count',
         'At least one group is needed.'),
        ({'playoff_qualifiers_per_group': 0}, 'playoff_qualifiers_per_group',
         'At least one contestant must advance from each group.'),
        ({'playoff_qualifiers_per_group': 4}, 'playoff_qualifiers_per_group',
         'Fewer must advance from each group than the smallest group holds.'),
        ({'playoff_group_count': 5}, 'playoff_group_count',
         GROUPS_TOO_MANY),
        ({'playoff_qualifier_count': 4}, 'playoff_qualifier_count',
         'Only used for highscore playoffs.'),
        ({'playoff_game_format': FFA}, 'playoff_game_format',
         'Round robin playoffs must be 1v1.'),
        ({'playoff_elimination_mode': RR}, 'playoff_elimination_mode',
         'Playoffs must use single or double elimination.'),
        ({'playoff_release_mode': None}, 'playoff_release_mode',
         'Please choose how the playoffs are released.'),
    ],
)
# fmt: on
def test_rr_playoff_config_rules(overrides, field, msgid):
    assert _msgids(_rr_settings(**overrides)) == {field: msgid}


# fmt: off
@pytest.mark.parametrize(
    ('mode', 'per_group', 'min_players'),
    [
        (SE, 2, 4),
        (DE, 4, 6),
        (DE, 2, 4),
    ],
)
# fmt: on
def test_rr_playoffs_accept_one_group(mode, per_group, min_players):
    settings = _rr_settings(
        playoff_elimination_mode=mode,
        playoff_group_count=1,
        playoff_qualifiers_per_group=per_group,
        min_players=min_players,
    )
    assert _msgids(settings) == {}


# fmt: off
@pytest.mark.parametrize(
    ('overrides', 'field', 'msgid'),
    [
        ({'playoff_qualifiers_per_group': 4, 'min_players': 4},
         'playoff_qualifiers_per_group',
         'Fewer must advance from each group than the smallest group holds.'),
        ({'playoff_qualifiers_per_group': 1, 'min_players': 4},
         'playoff_qualifiers_per_group', PLAYOFFS_TOO_FEW),
        ({'min_players': 1}, 'playoff_group_count', GROUPS_TOO_MANY),
    ],
)
# fmt: on
def test_rr_one_group_still_validates_qualifiers(overrides, field, msgid):
    settings = _rr_settings(
        playoff_elimination_mode=SE, playoff_group_count=1, **overrides
    )
    assert _msgids(settings) == {field: msgid}


def test_rr_playoffs_use_the_team_minimum_for_team_tournaments():
    settings = _rr_settings(
        contestant_type=ContestantType.TEAM,
        min_players=2,
        min_teams=4,
        playoff_group_count=2,
        playoff_qualifiers_per_group=2,
    )
    assert _msgids(settings) == {
        'playoff_qualifiers_per_group': (
            'Fewer must advance from each group than the smallest group '
            'holds.'
        )
    }


# fmt: off
@pytest.mark.parametrize(
    ('mode', 'groups', 'per_group', 'msgid'),
    [
        (DE, 2, 1, None),
        (DE, 3, 1, None),
        (DE, 1, 1, PLAYOFFS_TOO_FEW),
        (DE, 2, 2, None),
        (SE, 2, 1, None),
    ],
)
# fmt: on
def test_de_playoffs_accept_the_single_elimination_fallback(
    mode, groups, per_group, msgid
):
    settings = _rr_settings(
        playoff_elimination_mode=mode,
        playoff_group_count=groups,
        playoff_qualifiers_per_group=per_group,
        min_players=16,
    )
    expected = {} if msgid is None else {'playoff_qualifiers_per_group': msgid}
    assert _msgids(settings) == expected


# fmt: off
@pytest.mark.parametrize(
    ('overrides', 'field', 'msgid'),
    [
        ({'playoff_qualifier_count': None}, 'playoff_qualifier_count',
         'Please enter the number of qualifiers.'),
        ({'playoff_qualifier_count': 1}, 'playoff_qualifier_count',
         'At least two qualifiers are needed.'),
        ({'playoff_qualifier_count': 3, 'group_size_min': 4},
         'playoff_qualifier_count',
         'Qualifiers must be at least the minimum group size.'),
        ({'playoff_group_count': 2}, 'playoff_group_count',
         'Only used for round robin playoffs.'),
        ({'playoff_qualifiers_per_group': 1}, 'playoff_group_count',
         'Only used for round robin playoffs.'),
        ({'playoff_game_format': ONE}, 'playoff_game_format',
         'Highscore playoffs must be Free-for-All.'),
        ({'playoff_elimination_mode': None}, 'playoff_elimination_mode',
         'Please choose a playoff elimination mode.'),
        ({'playoff_point_table_missing': True}, 'point_table',
         'Add points for at least place 1.'),
    ],
)
# fmt: on
def test_highscore_playoffs_need_k(overrides, field, msgid):
    if overrides.pop('playoff_point_table_missing', False):
        overrides['point_table'] = None
    assert _msgids(_hs_settings(**overrides)) == {field: msgid}


# fmt: off
@pytest.mark.parametrize(
    ('qualifiers', 'minimum', 'maximum', 'accepted'),
    [
        (5, 3, 4, False),
        (9, 4, 4, False),
        (8, 3, 4, True),
        (7, 3, 4, True),
        (6, 3, 3, True),
        (2, 2, 4, True),
    ],
)
# fmt: on
def test_highscore_qualifiers_must_split_into_lobbies(
    qualifiers, minimum, maximum, accepted
):
    settings = _hs_settings(
        playoff_qualifier_count=qualifiers,
        group_size_min=minimum,
        group_size_max=maximum,
    )

    expected = (
        {}
        if accepted
        else {
            'playoff_qualifier_count': (
                'The qualifiers cannot be split into lobbies between the '
                'minimum and maximum group size.'
            )
        }
    )
    assert _msgids(settings) == expected


def _stall_params(settings: TournamentSettings) -> dict[str, Any]:
    result = validate_tournament_settings(settings, require_structure=True)
    assert result.is_err()
    message = result.unwrap_err()['playoff_qualifier_count']
    return {'msgid': message.msgid, **dict(message.params)}


def test_highscore_ffa_se_qualifiers_that_stall_are_refused():
    settings = _hs_settings(
        playoff_qualifier_count=8,
        group_size_min=4,
        group_size_max=4,
        advancement_count=3,
    )

    assert _stall_params(settings) == {
        'msgid': tournament_domain_service.HIGHSCORE_FFA_STALLS_MSGID,
        'cut': 3,
        'count': 6,
        'round': 2,
        'sizes': '3, 3',
        'minimum': 4,
    }


def test_highscore_ffa_de_is_not_checked_for_stalls():
    settings = _hs_settings(
        playoff_elimination_mode=DE,
        playoff_qualifier_count=8,
        group_size_min=4,
        group_size_max=4,
        advancement_count=3,
    )

    assert 'playoff_qualifier_count' not in _msgids(settings)


def test_highscore_ffa_se_that_never_shrinks_is_refused():
    settings = _hs_settings(
        playoff_qualifier_count=8,
        group_size_min=None,
        group_size_max=4,
        advancement_count=3,
    )

    assert _stall_params(settings) == {
        'msgid': tournament_domain_service.HIGHSCORE_FFA_NO_PROGRESS_MSGID,
        'cut': 3,
        'count': 6,
        'round': 3,
        'sizes': '3, 3',
        'minimum': 2,
    }


# fmt: off
@pytest.mark.parametrize(
    ('game_format', 'mode'),
    [
        (ONE, SE),
        (ONE, DE),
        (FFA, SE),
        (None, None),
    ],
)
@pytest.mark.parametrize(
    'field',
    [
        'playoff_game_format',
        'playoff_elimination_mode',
        'playoff_group_count',
        'playoff_qualifiers_per_group',
        'playoff_qualifier_count',
        'playoff_release_mode',
    ],
)
# fmt: on
def test_other_format_rejects_playoff_fields(game_format, mode, field):
    base = {
        'game_format': game_format,
        'elimination_mode': mode,
        'contestant_type': ContestantType.SOLO,
        'playoff_game_format': None,
        'playoff_elimination_mode': None,
        'playoff_group_count': None,
        'playoff_qualifiers_per_group': None,
        'playoff_qualifier_count': None,
        'playoff_release_mode': None,
        'point_table': [3, 2, 1] if game_format == FFA else None,
        'group_size_max': 4 if game_format == FFA else None,
    }
    value = {
        'playoff_game_format': ONE,
        'playoff_elimination_mode': SE,
        'playoff_release_mode': PlayoffReleaseMode.MANUAL,
    }.get(field, 3)
    base[field] = value
    settings = _rr_settings(**base)

    errors = _msgids(settings)

    assert errors['playoff_game_format'] == 'This format has no playoff phase.'


def test_round_robin_with_another_mode_has_no_playoffs():
    settings = _rr_settings(elimination_mode=SE)
    assert _msgids(settings) == {
        'playoff_game_format': 'This format has no playoff phase.'
    }


# --- phase helpers --------------------------------------------------- #


def _tournament(**overrides) -> Tournament:
    fields: dict[str, Any] = {
        'id': TournamentID(generate_uuid()),
        'party_id': 'test-party',
        'name': 'Playoff Cup',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': datetime.now(UTC),
        'min_players': 8,
        'max_players': 16,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': ContestantType.SOLO,
        'tournament_status': TournamentStatus.REGISTRATION_OPEN,
        'game_format': ONE,
        'elimination_mode': RR,
        'playoff_game_format': ONE,
        'playoff_elimination_mode': DE,
        'playoff_group_count': 2,
        'playoff_qualifiers_per_group': 2,
        'playoff_release_mode': PlayoffReleaseMode.MANUAL,
    }
    fields.update(overrides)
    return Tournament(**fields)


# fmt: off
@pytest.mark.parametrize(
    ('phase', 'expected_format', 'expected_mode'),
    [
        (1, ONE, RR),
        (2, ONE, DE),
        (0, None, None),
        (3, None, None),
    ],
)
# fmt: on
def test_phase_helpers(phase, expected_format, expected_mode):
    tournament = _tournament()
    assert (
        tournament_domain_service.game_format_for_phase(tournament, phase)
        == expected_format
    )
    assert (
        tournament_domain_service.elimination_mode_for_phase(tournament, phase)
        == expected_mode
    )


def test_phase_two_helpers_are_none_without_playoffs():
    tournament = _tournament(
        playoff_game_format=None,
        playoff_elimination_mode=None,
        playoff_group_count=None,
        playoff_qualifiers_per_group=None,
        playoff_release_mode=None,
    )
    assert tournament_domain_service.game_format_for_phase(tournament, 2) is None
    assert (
        tournament_domain_service.elimination_mode_for_phase(tournament, 2)
        is None
    )


# --- service enforcement --------------------------------------------- #


def _update_kwargs(tournament: Tournament) -> dict:
    return {
        'name': tournament.name,
        'game': tournament.game,
        'start_time': tournament.start_time,
        'min_players': tournament.min_players,
        'max_players': tournament.max_players,
        'contestant_type': tournament.contestant_type,
        'game_format': tournament.game_format,
        'elimination_mode': tournament.elimination_mode,
    }


@patch(_SIGNALS)
@patch(_REPO)
def test_update_rejects_an_invalid_playoff_config(repo, signals):
    repo.get_tournament.return_value = _tournament()

    result = tournament_service.update_tournament(
        repo.get_tournament.return_value.id,
        **_update_kwargs(repo.get_tournament.return_value),
        playoff_group_count=0,
    )

    assert result == Err(ValidationMessage('At least one group is needed.'))
    repo.update_tournament.assert_not_called()


@patch(_SIGNALS)
@patch(_REPO)
def test_update_returns_parameterised_messages_unformatted(repo, signals):
    tournament = _tournament(
        game_format=HS,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        playoff_game_format=FFA,
        playoff_group_count=None,
        playoff_qualifiers_per_group=None,
        playoff_qualifier_count=8,
        point_table=[1],
        group_size_max=4,
    )
    repo.get_tournament.return_value = tournament
    kwargs = _update_kwargs(tournament)
    kwargs['point_table'] = [1] * 65

    result = tournament_service.update_tournament(tournament.id, **kwargs)

    assert result == Err(
        ValidationMessage('At most %(max)s places.', (('max', 64),))
    )


@patch(_SIGNALS)
@patch(_REPO)
def test_update_without_playoff_kwargs_keeps_the_stored_config(repo, signals):
    tournament = _tournament()
    repo.get_tournament.return_value = tournament

    result = tournament_service.update_tournament(
        tournament.id, **_update_kwargs(tournament)
    )

    assert result.is_ok()
    saved = repo.update_tournament.call_args.args[0]
    assert saved.playoff_game_format == ONE
    assert saved.playoff_elimination_mode == DE
    assert saved.playoff_group_count == 2
    assert saved.playoff_qualifiers_per_group == 2
    assert saved.playoff_release_mode == PlayoffReleaseMode.MANUAL


@patch(_SIGNALS)
@patch(_REPO)
def test_update_clears_the_playoff_config_with_explicit_none(repo, signals):
    tournament = _tournament()
    repo.get_tournament.return_value = tournament

    result = tournament_service.update_tournament(
        tournament.id,
        **_update_kwargs(tournament),
        playoff_game_format=None,
        playoff_elimination_mode=None,
        playoff_group_count=None,
        playoff_qualifiers_per_group=None,
        playoff_qualifier_count=None,
        playoff_release_mode=None,
    )

    assert result.is_ok()
    saved = repo.update_tournament.call_args.args[0]
    assert not saved.has_playoffs
    assert saved.playoff_group_count is None


@patch(_SIGNALS)
@patch(_REPO)
def test_update_never_carries_release_state_from_the_caller(repo, signals):
    released_at = datetime(2026, 9, 1, tzinfo=UTC)
    tournament = _tournament(
        playoff_released_at=released_at, playoff_auto_release_suspended=True
    )
    repo.get_tournament.return_value = tournament

    with pytest.raises(TypeError):
        tournament_service.update_tournament(
            tournament.id,
            **_update_kwargs(tournament),
            playoff_released_at=None,
        )


_RELEASED = datetime(2026, 9, 1, 12, 0)


# fmt: off
@pytest.mark.parametrize(
    ('kwarg', 'value'),
    [
        ('playoff_elimination_mode', SE),
        ('playoff_qualifiers_per_group', 3),
        ('playoff_release_mode', PlayoffReleaseMode.AUTOMATIC),
    ],
)
# fmt: on
@pytest.mark.parametrize(
    'status', [TournamentStatus.ONGOING, TournamentStatus.PAUSED]
)
@patch(_SIGNALS)
@patch(_REPO)
def test_playoff_settings_stay_editable_until_the_release(
    repo, signals, status, kwarg, value
):
    tournament = _tournament(tournament_status=status, min_players=16)
    repo.get_tournament.return_value = tournament
    kwargs = _update_kwargs(tournament)
    kwargs[kwarg] = value

    result = tournament_service.update_tournament(tournament.id, **kwargs)

    assert result.is_ok(), result.unwrap_err()
    assert getattr(repo.update_tournament.call_args.args[0], kwarg) == value


# fmt: off
@pytest.mark.parametrize(
    ('kwarg', 'value'),
    [
        ('playoff_game_format', None),
        ('playoff_group_count', 4),
    ],
)
# fmt: on
@pytest.mark.parametrize(
    'status',
    [
        TournamentStatus.ONGOING,
        TournamentStatus.PAUSED,
        TournamentStatus.COMPLETED,
    ],
)
@patch(_SIGNALS)
@patch(_REPO)
def test_playoff_switch_and_group_count_lock_once_started(
    repo, signals, status, kwarg, value
):
    tournament = _tournament(tournament_status=status, min_players=16)
    repo.get_tournament.return_value = tournament
    kwargs = _update_kwargs(tournament)
    kwargs[kwarg] = value
    if kwarg == 'playoff_game_format':
        kwargs.update(
            playoff_elimination_mode=None,
            playoff_group_count=None,
            playoff_qualifiers_per_group=None,
            playoff_release_mode=None,
        )

    result = tournament_service.update_tournament(tournament.id, **kwargs)

    assert result == Err(tournament_service.PLAYOFF_STARTED_EDIT_ERROR)
    repo.update_tournament.assert_not_called()


@pytest.mark.parametrize(
    'status',
    [
        TournamentStatus.ONGOING,
        TournamentStatus.PAUSED,
        TournamentStatus.COMPLETED,
    ],
)
@patch(_SIGNALS)
@patch(_REPO)
def test_playoff_settings_lock_after_the_release(repo, signals, status):
    tournament = _tournament(
        tournament_status=status, min_players=16, playoff_released_at=_RELEASED
    )
    repo.get_tournament.return_value = tournament

    result = tournament_service.update_tournament(
        tournament.id,
        **_update_kwargs(tournament),
        playoff_qualifiers_per_group=3,
    )

    assert result == Err(tournament_service.PLAYOFF_RELEASED_EDIT_ERROR)
    repo.update_tournament.assert_not_called()


@patch(_SIGNALS)
@patch(_REPO)
def test_completed_without_release_keeps_the_cut_editable(repo, signals):
    tournament = _tournament(
        tournament_status=TournamentStatus.COMPLETED, min_players=16
    )
    repo.get_tournament.return_value = tournament

    result = tournament_service.update_tournament(
        tournament.id,
        **_update_kwargs(tournament),
        playoff_qualifiers_per_group=3,
    )

    assert result.is_ok(), result.unwrap_err()


def _hs_tournament(**overrides) -> Tournament:
    return _tournament(
        game_format=HS,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        point_table=[3, 2, 1],
        group_size_min=2,
        group_size_max=4,
        playoff_game_format=FFA,
        playoff_elimination_mode=SE,
        playoff_group_count=None,
        playoff_qualifiers_per_group=None,
        playoff_qualifier_count=8,
        **overrides,
    )


def _hs_kwargs(tournament: Tournament) -> dict:
    kwargs = _update_kwargs(tournament)
    kwargs.update(
        score_ordering=tournament.score_ordering,
        point_table=tournament.point_table,
        group_size_min=tournament.group_size_min,
        group_size_max=tournament.group_size_max,
    )
    return kwargs


@patch(_SIGNALS)
@patch(_REPO)
def test_highscore_playoff_settings_stay_editable_until_the_release(
    repo, signals
):
    tournament = _hs_tournament(tournament_status=TournamentStatus.ONGOING)
    repo.get_tournament.return_value = tournament
    kwargs = _hs_kwargs(tournament)
    kwargs['group_size_max'] = 5

    result = tournament_service.update_tournament(
        tournament.id, **kwargs, playoff_qualifier_count=4
    )

    assert result.is_ok(), result.unwrap_err()
    saved = repo.update_tournament.call_args.args[0]
    assert (saved.playoff_qualifier_count, saved.group_size_max) == (4, 5)


@patch(_SIGNALS)
@patch(_REPO)
def test_highscore_playoff_settings_lock_after_the_release(repo, signals):
    tournament = _hs_tournament(
        tournament_status=TournamentStatus.COMPLETED,
        playoff_released_at=_RELEASED,
    )
    repo.get_tournament.return_value = tournament
    kwargs = _hs_kwargs(tournament)
    kwargs['group_size_max'] = 5

    result = tournament_service.update_tournament(tournament.id, **kwargs)

    assert result == Err(tournament_service.PLAYOFF_RELEASED_EDIT_ERROR)
    repo.rollback_session.assert_called_once()


@patch(_SIGNALS)
@patch(_REPO)
def test_a_refused_started_edit_rolls_back(repo, signals):
    tournament = _tournament(
        tournament_status=TournamentStatus.ONGOING, min_players=16
    )
    repo.get_tournament.return_value = tournament

    result = tournament_service.update_tournament(
        tournament.id,
        **_update_kwargs(tournament),
        playoff_group_count=4,
    )

    assert result == Err(tournament_service.PLAYOFF_STARTED_EDIT_ERROR)
    repo.rollback_session.assert_called_once()


_AUTO_RELEASE = (
    'byceps.services.lan_tournament.tournament_qualification_service'
    '.try_auto_release'
)


# fmt: off
@pytest.mark.parametrize(
    ('status', 'initiator', 'changes', 'expected'),
    [
        (TournamentStatus.ONGOING, True, {'playoff_qualifiers_per_group': 3}, 1),
        (TournamentStatus.ONGOING, True, {}, 0),
        (TournamentStatus.ONGOING, False, {'playoff_qualifiers_per_group': 3}, 0),
        (TournamentStatus.PAUSED, True, {'playoff_qualifiers_per_group': 3}, 0),
    ],
)
# fmt: on
@patch(_SIGNALS)
@patch(_REPO)
def test_a_playoff_edit_of_an_ongoing_tournament_tries_the_release(
    repo, signals, status, initiator, changes, expected
):
    tournament = _tournament(tournament_status=status, min_players=16)
    repo.get_tournament.return_value = tournament
    user_id = generate_uuid() if initiator else None

    with patch(_AUTO_RELEASE) as try_auto_release:
        result = tournament_service.update_tournament(
            tournament.id,
            **_update_kwargs(tournament),
            **changes,
            initiator_id=user_id,
        )

    assert result.is_ok(), result.unwrap_err()
    assert try_auto_release.call_count == expected
    if expected:
        try_auto_release.assert_called_once_with(
            tournament.id, triggered_by=user_id
        )


@patch(_SIGNALS)
@patch(_REPO)
def test_unchanged_playoff_config_does_not_lock_an_edit(repo, signals):
    tournament = _tournament(tournament_status=TournamentStatus.ONGOING)
    repo.get_tournament.return_value = tournament

    result = tournament_service.update_tournament(
        tournament.id,
        **_update_kwargs(tournament),
        description='New text',
        playoff_group_count=2,
    )

    assert result.is_ok()


@patch(_SIGNALS)
@patch(_REPO)
def test_create_rejects_an_invalid_playoff_config(repo, signals):
    repo.get_max_position_for_party.return_value = 0

    result = tournament_service.create_tournament(
        'test-party',
        'Playoff Cup',
        min_players=8,
        max_players=16,
        contestant_type=ContestantType.SOLO,
        game_format=ONE,
        elimination_mode=RR,
        playoff_game_format=ONE,
        playoff_elimination_mode=DE,
        playoff_group_count=1,
        playoff_qualifiers_per_group=1,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )

    assert result == Err(ValidationMessage(PLAYOFFS_TOO_FEW))
    repo.create_tournament.assert_not_called()


@patch(_SIGNALS)
@patch(_REPO)
def test_create_rejects_playoffs_on_a_format_without_them(repo, signals):
    repo.get_max_position_for_party.return_value = 0

    result = tournament_service.create_tournament(
        'test-party',
        'Bracket Cup',
        contestant_type=ContestantType.SOLO,
        game_format=ONE,
        elimination_mode=SE,
        playoff_group_count=2,
    )

    assert result == Err(ValidationMessage('This format has no playoff phase.'))
    repo.create_tournament.assert_not_called()


@patch(_SIGNALS)
@patch(_REPO)
def test_create_stores_a_valid_playoff_config(repo, signals):
    repo.get_max_position_for_party.return_value = 0

    result = tournament_service.create_tournament(
        'test-party',
        'Playoff Cup',
        min_players=8,
        max_players=16,
        contestant_type=ContestantType.SOLO,
        game_format=ONE,
        elimination_mode=RR,
        playoff_game_format=ONE,
        playoff_elimination_mode=SE,
        playoff_group_count=2,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
    )

    assert isinstance(result, Ok)
    tournament, _ = result.unwrap()
    assert tournament.playoff_group_count == 2
    assert tournament.playoff_release_mode == PlayoffReleaseMode.AUTOMATIC
    repo.create_tournament.assert_called_once()
