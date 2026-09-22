"""
tests.unit.services.lan_tournament.test_site_orga_actions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from flask import Flask
import pytest
from werkzeug.exceptions import NotFound

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


TOURNAMENT_ID = TournamentID(generate_uuid())
MATCH_ID = TournamentMatchID(generate_uuid())
MATCH_ID_STR = str(MATCH_ID)
PARTY_ID_STR = str(generate_uuid())
USER_ID = generate_uuid()

# Two solo contestants, A and B.
PARTICIPANT_A = TournamentParticipantID(generate_uuid())
PARTICIPANT_B = TournamentParticipantID(generate_uuid())

_V = 'byceps.services.lan_tournament.blueprints.site.views'


# ------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------ #


@pytest.fixture(scope='module')
def app():
    """Minimal Flask app for `test_request_context`."""
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    return a


def _make_tournament(
    contestant_type: ContestantType = ContestantType.SOLO,
    status: TournamentStatus = TournamentStatus.ONGOING,
    party_id: str = PARTY_ID_STR,
    game_format: GameFormat = GameFormat.ONE_V_ONE,
) -> MagicMock:
    t = MagicMock(spec=Tournament)
    t.id = TOURNAMENT_ID
    t.party_id = party_id
    t.contestant_type = contestant_type
    t.tournament_status = status
    t.game_format = game_format
    return t


def _make_match(confirmed: bool = False) -> MagicMock:
    m = MagicMock(spec=TournamentMatch)
    m.id = MATCH_ID
    m.tournament_id = TOURNAMENT_ID
    m.confirmed_by = USER_ID if confirmed else None
    return m


def _make_contestant(
    participant_id=None, team_id=None, score=None
) -> MagicMock:
    c = MagicMock()
    c.participant_id = participant_id
    c.team_id = team_id
    c.score = score
    return c


def _raw(view_func):
    """Unwrap `@login_required` and `@scoped_orga_required`."""
    return view_func.__wrapped__.__wrapped__


@contextmanager
def _patched_orga_view(
    app,
    *,
    contestants=None,
    tournament=None,
    match=None,
    confirm_result=None,
    unconfirm_result=None,
    correct_result=None,
    status_result=None,
    set_placements_result=None,
    confirm_ffa_result=None,
):
    """Patch the dependencies of the scoped-orga action views."""
    if tournament is None:
        tournament = _make_tournament()
    if match is None:
        match = _make_match()
    if contestants is None:
        contestants = []

    with (
        app.app_context(),
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        # The score parse lives in the shared view helper.
        patch(
            'byceps.services.lan_tournament.lan_tournament_view_helpers.gettext',
            side_effect=lambda msg, **kw: msg,
        ),
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
        patch(f'{_V}.g') as mock_g,
    ):
        mock_get_tournament.return_value = tournament
        mock_match_svc.get_match.return_value = match
        mock_match_svc.get_contestants_for_match.return_value = contestants
        mock_match_svc.admin_set_and_confirm_match.return_value = (
            confirm_result if confirm_result is not None else Ok(None)
        )
        mock_match_svc.unconfirm_match.return_value = (
            unconfirm_result if unconfirm_result is not None else Ok(None)
        )
        mock_match_svc.correct_match_result.return_value = (
            correct_result if correct_result is not None else Ok((None, False))
        )
        mock_match_svc.add_comment.return_value = Ok(None)
        mock_match_svc.set_ffa_placements.return_value = (
            set_placements_result
            if set_placements_result is not None
            else Ok(None)
        )
        mock_match_svc.confirm_ffa_match.return_value = (
            confirm_ffa_result if confirm_ffa_result is not None else Ok(None)
        )
        mock_tournament_svc.change_status.return_value = (
            status_result
            if status_result is not None
            else Ok((tournament, MagicMock()))
        )

        mock_g.user.id = USER_ID
        mock_g.user.authenticated = True
        mock_g.user.has_permission.return_value = False

        yield {
            'flash_error': mock_flash_error,
            'flash_success': mock_flash_success,
            'redirect_to': mock_redirect_to,
            'match_svc': mock_match_svc,
            'tournament_svc': mock_tournament_svc,
            'get_tournament': mock_get_tournament,
        }


def _call_confirm(app, form_data):
    from byceps.services.lan_tournament.blueprints.site import views

    raw_fn = _raw(views.orga_confirm_match_with_scores)
    with app.test_request_context('/', method='POST', data=form_data):
        return raw_fn(MATCH_ID_STR)


def _call_unconfirm(app, form_data):
    from byceps.services.lan_tournament.blueprints.site import views

    raw_fn = _raw(views.orga_unconfirm_match)
    with app.test_request_context('/', method='POST', data=form_data):
        return raw_fn(MATCH_ID_STR)


def _call_correct(app, form_data):
    from byceps.services.lan_tournament.blueprints.site import views

    raw_fn = _raw(views.orga_correct_match_result)
    with app.test_request_context('/', method='POST', data=form_data):
        return raw_fn(MATCH_ID_STR)


def _call_set_placements(app, form_data):
    from byceps.services.lan_tournament.blueprints.site import views

    raw_fn = _raw(views.orga_set_ffa_placements)
    with app.test_request_context('/', method='POST', data=form_data):
        return raw_fn(MATCH_ID_STR)


def _call_confirm_ffa(app, form_data):
    from byceps.services.lan_tournament.blueprints.site import views

    raw_fn = _raw(views.orga_confirm_ffa_match)
    with app.test_request_context('/', method='POST', data=form_data):
        return raw_fn(MATCH_ID_STR)


def _call_status(app, tournament_id, action):
    from byceps.services.lan_tournament.blueprints.site import views

    raw_fn = _raw(views.orga_change_tournament_status)
    with app.test_request_context('/', method='POST'):
        return raw_fn(tournament_id, action)


# ------------------------------------------------------------------ #
# score binding by contestant key
# ------------------------------------------------------------------ #


def test_orga_confirm_binds_scores_by_key_not_position(app):
    form_data = {
        f'score_{PARTICIPANT_A}': '3',
        f'score_{PARTICIPANT_B}': '1',
    }
    # Iteration order is reversed relative to the submitted fields.
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_B, score=0),
        _make_contestant(participant_id=PARTICIPANT_A, score=0),
    ]

    with _patched_orga_view(app, contestants=contestants) as mocks:
        _call_confirm(app, form_data)

    mocks['match_svc'].admin_set_and_confirm_match.assert_called_once()
    call = mocks['match_svc'].admin_set_and_confirm_match.call_args
    scores = call.args[2]

    assert scores == {PARTICIPANT_A: 3, PARTICIPANT_B: 1}


def test_orga_confirm_rejects_missing_score(app):
    form_data = {f'score_{PARTICIPANT_A}': '3'}
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A),
        _make_contestant(participant_id=PARTICIPANT_B),
    ]

    with _patched_orga_view(app, contestants=contestants) as mocks:
        _call_confirm(app, form_data)

    mocks['match_svc'].admin_set_and_confirm_match.assert_not_called()
    mocks['flash_error'].assert_called_once()


# ------------------------------------------------------------------ #
# unconfirm
# ------------------------------------------------------------------ #


def test_orga_unconfirm_requires_reason(app):
    with _patched_orga_view(app, match=_make_match(confirmed=True)) as mocks:
        _call_unconfirm(app, {'reason': ''})

    mocks['match_svc'].unconfirm_match.assert_not_called()
    mocks['flash_error'].assert_called_once()


def test_orga_unconfirm_passes_reason_through(app):
    with _patched_orga_view(
        app,
        tournament=_make_tournament(game_format=GameFormat.FREE_FOR_ALL),
        match=_make_match(confirmed=True),
    ) as mocks:
        _call_unconfirm(app, {'reason': '  scorekeeper error  '})

    mocks['match_svc'].unconfirm_match.assert_called_once_with(
        MATCH_ID, USER_ID, reason='scorekeeper error'
    )


def test_orga_unconfirm_refuses_bracket_match(app):
    """Send bracket matches to the correction panel instead."""
    with _patched_orga_view(app, match=_make_match(confirmed=True)) as mocks:
        _call_unconfirm(app, {'reason': 'scorekeeper error'})

    mocks['match_svc'].unconfirm_match.assert_not_called()
    mocks['flash_error'].assert_called_once_with(
        'Bracket matches are retracted in the result correction panel, '
        'which requires acknowledging the impact on downstream matches.'
    )


# ------------------------------------------------------------------ #
# correction
# ------------------------------------------------------------------ #


def test_orga_correct_result_passes_ack_critical_through(app):
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A, score=1),
        _make_contestant(participant_id=PARTICIPANT_B, score=0),
    ]
    form_data = {
        'reason': 'downstream already confirmed',
        'ack_critical': 'y',
        f'corrected_score_{PARTICIPANT_A}': '3',
        f'corrected_score_{PARTICIPANT_B}': '1',
    }

    with _patched_orga_view(
        app, contestants=contestants, match=_make_match(confirmed=True)
    ) as mocks:
        _call_correct(app, form_data)

    mocks['match_svc'].correct_match_result.assert_called_once()
    kwargs = mocks['match_svc'].correct_match_result.call_args.kwargs
    assert kwargs['ack_critical'] is True


def test_orga_correct_result_ack_critical_defaults_false(app):
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A, score=1),
        _make_contestant(participant_id=PARTICIPANT_B, score=0),
    ]
    form_data = {
        'reason': 'typo fix',
        f'corrected_score_{PARTICIPANT_A}': '2',
        f'corrected_score_{PARTICIPANT_B}': '0',
    }

    with _patched_orga_view(
        app, contestants=contestants, match=_make_match(confirmed=True)
    ) as mocks:
        _call_correct(app, form_data)

    kwargs = mocks['match_svc'].correct_match_result.call_args.kwargs
    assert kwargs['ack_critical'] is False


def test_orga_correct_result_passes_acknowledged_match_ids(app):
    shown_1 = TournamentMatchID(generate_uuid())
    shown_2 = TournamentMatchID(generate_uuid())
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A, score=1),
        _make_contestant(participant_id=PARTICIPANT_B, score=0),
    ]
    form_data = {
        'reason': 'downstream already confirmed',
        'ack_critical': 'y',
        'ack_match_ids': f'{shown_1},{shown_2}',
        f'corrected_score_{PARTICIPANT_A}': '3',
        f'corrected_score_{PARTICIPANT_B}': '1',
    }

    with _patched_orga_view(
        app, contestants=contestants, match=_make_match(confirmed=True)
    ) as mocks:
        _call_correct(app, form_data)

    kwargs = mocks['match_svc'].correct_match_result.call_args.kwargs
    assert kwargs['acknowledged_match_ids'] == [shown_1, shown_2]


def test_orga_correct_result_refuses_ffa_match(app):
    with _patched_orga_view(
        app,
        tournament=_make_tournament(game_format=GameFormat.FREE_FOR_ALL),
        match=_make_match(confirmed=True),
    ) as mocks:
        _call_correct(app, {'reason': 'typo fix'})

    mocks['match_svc'].correct_match_result.assert_not_called()
    mocks['flash_error'].assert_called_once_with(
        'Free-for-all matches are corrected by unconfirming them and '
        're-entering the placements.'
    )


def test_orga_correct_result_error_is_translated_whole(app):
    service_error = 'Match is not confirmed.'
    catalogue = {
        'Error correcting match result: %(error)s Nothing was changed; '
        'the original result remains confirmed.': (
            'Fehler bei der Korrektur des Partieergebnisses: %(error)s '
            'Es wurde nichts geändert; das ursprüngliche Ergebnis '
            'bleibt bestätigt.'
        ),
        service_error: 'Die Partie ist nicht bestätigt.',
    }

    def translate(msg, **kw):
        translated = catalogue.get(msg, msg)
        return translated % kw if kw else translated

    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A, score=1),
        _make_contestant(participant_id=PARTICIPANT_B, score=0),
    ]
    form_data = {
        'reason': 'typo fix',
        f'corrected_score_{PARTICIPANT_A}': '2',
        f'corrected_score_{PARTICIPANT_B}': '0',
    }

    with (
        _patched_orga_view(
            app,
            contestants=contestants,
            match=_make_match(confirmed=True),
            correct_result=Err(service_error),
        ) as mocks,
        patch(f'{_V}.gettext', side_effect=translate),
    ):
        _call_correct(app, form_data)

    (flashed,) = mocks['flash_error'].call_args.args
    assert flashed.startswith('Fehler bei der Korrektur')
    assert 'Die Partie ist nicht bestätigt' in flashed
    assert 'Match is not confirmed' not in flashed


def _translating(catalogue):
    def translate(msg, **kw):
        translated = catalogue.get(msg, msg)
        return translated % kw if kw else translated

    return patch(f'{_V}.gettext', side_effect=translate)


_SERVICE_ERROR = 'Match is not confirmed.'
_SERVICE_ERROR_DE = 'Die Partie ist nicht bestätigt.'


def _assert_flash_translated(mocks, wrapper_de):
    (flashed,) = mocks['flash_error'].call_args.args
    assert flashed == wrapper_de % {'error': _SERVICE_ERROR_DE}


def test_orga_confirm_error_is_translated_whole(app):
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A),
        _make_contestant(participant_id=PARTICIPANT_B),
    ]
    form_data = {
        f'score_{PARTICIPANT_A}': '3',
        f'score_{PARTICIPANT_B}': '1',
    }
    catalogue = {
        'Error confirming match: %(error)s': (
            'Fehler beim Bestätigen der Partie: %(error)s'
        ),
        _SERVICE_ERROR: _SERVICE_ERROR_DE,
    }

    with (
        _patched_orga_view(
            app,
            contestants=contestants,
            confirm_result=Err(_SERVICE_ERROR),
        ) as mocks,
        _translating(catalogue),
    ):
        _call_confirm(app, form_data)

    _assert_flash_translated(
        mocks, 'Fehler beim Bestätigen der Partie: %(error)s'
    )


def test_orga_unconfirm_error_is_translated_whole(app):
    catalogue = {
        'Error unconfirming match: %(error)s': (
            'Fehler beim Aufheben der Bestätigung: %(error)s'
        ),
        _SERVICE_ERROR: _SERVICE_ERROR_DE,
    }

    with (
        _patched_orga_view(
            app,
            tournament=_make_tournament(game_format=GameFormat.FREE_FOR_ALL),
            match=_make_match(confirmed=True),
            unconfirm_result=Err(_SERVICE_ERROR),
        ) as mocks,
        _translating(catalogue),
    ):
        _call_unconfirm(app, {'reason': 'scorekeeper error'})

    _assert_flash_translated(
        mocks, 'Fehler beim Aufheben der Bestätigung: %(error)s'
    )


def test_orga_comment_error_is_translated_whole(app):
    from byceps.services.lan_tournament.blueprints.site import views

    catalogue = {
        'Error adding comment: %(error)s': (
            'Fehler beim Hinzufügen des Kommentars: %(error)s'
        ),
        _SERVICE_ERROR: _SERVICE_ERROR_DE,
    }

    with (
        _patched_orga_view(app) as mocks,
        _translating(catalogue),
    ):
        mocks['match_svc'].add_comment.return_value = Err(_SERVICE_ERROR)
        raw_fn = _raw(views.orga_add_match_comment)
        with app.test_request_context(
            '/', method='POST', data={'comment': 'hello'}
        ):
            raw_fn(MATCH_ID_STR)

    _assert_flash_translated(
        mocks, 'Fehler beim Hinzufügen des Kommentars: %(error)s'
    )


def test_orga_status_change_error_is_translated_whole(app):
    catalogue = {
        'Status change failed: %(error)s': (
            'Statusänderung fehlgeschlagen: %(error)s'
        ),
        _SERVICE_ERROR: _SERVICE_ERROR_DE,
    }

    with (
        _patched_orga_view(app, status_result=Err(_SERVICE_ERROR)) as mocks,
        _translating(catalogue),
    ):
        _call_status(app, str(TOURNAMENT_ID), 'start')

    _assert_flash_translated(mocks, 'Statusänderung fehlgeschlagen: %(error)s')


# ------------------------------------------------------------------ #
# status-transition allow-list
# ------------------------------------------------------------------ #


def test_orga_status_transition_rejects_unlisted_action(app):
    for action in ('cancel', 'draft'):
        with _patched_orga_view(app) as mocks:
            with pytest.raises(NotFound):
                _call_status(app, str(TOURNAMENT_ID), action)

            mocks['tournament_svc'].change_status.assert_not_called()


def test_orga_status_transition_accepts_listed_action(app):
    with _patched_orga_view(app) as mocks:
        _call_status(app, str(TOURNAMENT_ID), 'start')

    mocks['tournament_svc'].change_status.assert_called_once_with(
        TOURNAMENT_ID, TournamentStatus.ONGOING, USER_ID
    )


# ------------------------------------------------------------------ #
# route surface
# ------------------------------------------------------------------ #


def test_scoped_orga_cannot_trigger_random_defwin():
    """Register exactly the listed `/orga/...` routes, and no defwin."""
    from byceps.services.lan_tournament.blueprints.site import views

    test_app = Flask(__name__)
    test_app.register_blueprint(views.blueprint, url_prefix='/lt')

    orga_endpoints = {
        rule.endpoint.rsplit('.', 1)[-1]
        for rule in test_app.url_map.iter_rules()
        if rule.endpoint.rsplit('.', 1)[-1].startswith('orga_')
    }

    assert orga_endpoints == {
        'orga_confirm_match_with_scores',
        'orga_unconfirm_match',
        'orga_correct_match_result',
        'orga_set_ffa_placements',
        'orga_confirm_ffa_match',
        'orga_add_match_comment',
        'orga_change_tournament_status',
    }
    assert not any('defwin' in endpoint for endpoint in orga_endpoints)


# ------------------------------------------------------------------ #
# cross-party isolation
# ------------------------------------------------------------------ #


def test_orga_action_rejects_tournament_from_another_party(app):
    """Answer 404 for a tournament of another party."""
    from byceps.services.lan_tournament.blueprints.site import views

    other_party_tournament = _make_tournament(
        party_id=str(generate_uuid())  # different from g.party.id below
    )
    match = _make_match()

    raw_fn = _raw(views.orga_unconfirm_match)

    with (
        app.test_request_context('/', method='POST', data={'reason': 'x'}),
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        # The score parse lives in the shared view helper.
        patch(
            'byceps.services.lan_tournament.lan_tournament_view_helpers.gettext',
            side_effect=lambda msg, **kw: msg,
        ),
        patch(f'{_V}.flash_error'),
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.g') as mock_g,
    ):
        mock_match_svc.get_match.return_value = match
        mock_tournament_svc.find_tournament.return_value = (
            other_party_tournament
        )
        mock_g.user.id = USER_ID
        mock_g.party.id = PARTY_ID_STR  # current site's party -- differs

        with pytest.raises(NotFound):
            raw_fn(MATCH_ID_STR)

        mock_match_svc.unconfirm_match.assert_not_called()


# ------------------------------------------------------------------ #
# results change only while the tournament is ongoing
# ------------------------------------------------------------------ #


_NOT_ONGOING = [
    status for status in TournamentStatus if status != TournamentStatus.ONGOING
]


@pytest.mark.parametrize('status', _NOT_ONGOING, ids=lambda s: s.name)
@pytest.mark.parametrize(
    ('call', 'service_function', 'game_format', 'confirmed', 'form_data'),
    [
        (
            _call_confirm,
            'admin_set_and_confirm_match',
            GameFormat.ONE_V_ONE,
            False,
            {},
        ),
        (
            _call_unconfirm,
            'unconfirm_match',
            GameFormat.FREE_FOR_ALL,
            True,
            {'reason': 'x'},
        ),
        (
            _call_correct,
            'correct_match_result',
            GameFormat.ONE_V_ONE,
            True,
            {'reason': 'x'},
        ),
        (
            _call_set_placements,
            'set_ffa_placements',
            GameFormat.FREE_FOR_ALL,
            False,
            {f'placement_{PARTICIPANT_A}': '1'},
        ),
        (
            _call_confirm_ffa,
            'confirm_ffa_match',
            GameFormat.FREE_FOR_ALL,
            False,
            {},
        ),
    ],
    ids=['confirm', 'unconfirm', 'correct', 'placements', 'confirm_ffa'],
)
def test_orga_result_action_refused_unless_ongoing(
    app, status, call, service_function, game_format, confirmed, form_data
):
    tournament = _make_tournament(status=status, game_format=game_format)

    with _patched_orga_view(
        app, tournament=tournament, match=_make_match(confirmed=confirmed)
    ) as mocks:
        call(app, form_data)

    getattr(mocks['match_svc'], service_function).assert_not_called()
    mocks['flash_error'].assert_called_once_with(
        'Tournament is not in progress.'
    )


# ------------------------------------------------------------------ #
# FFA placements and confirmation
# ------------------------------------------------------------------ #


_FFA = _make_tournament(game_format=GameFormat.FREE_FOR_ALL)


def test_orga_set_ffa_placements_binds_placements_by_contestant_key(app):
    form_data = {
        f'placement_{PARTICIPANT_A}': '2',
        f'placement_{PARTICIPANT_B}': '1',
    }

    with _patched_orga_view(app, tournament=_FFA) as mocks:
        _call_set_placements(app, form_data)

    mocks['match_svc'].set_ffa_placements.assert_called_once_with(
        MATCH_ID, {str(PARTICIPANT_A): 2, str(PARTICIPANT_B): 1}
    )
    mocks['flash_success'].assert_called_once_with('Placements have been set.')


def test_orga_set_ffa_placements_rejects_non_integer(app):
    form_data = {f'placement_{PARTICIPANT_A}': 'first'}

    with _patched_orga_view(app, tournament=_FFA) as mocks:
        _call_set_placements(app, form_data)

    mocks['match_svc'].set_ffa_placements.assert_not_called()
    mocks['flash_error'].assert_called_once_with(
        'Invalid placement value for contestant.'
    )


def test_orga_set_ffa_placements_rejects_empty_submission(app):
    with _patched_orga_view(app, tournament=_FFA) as mocks:
        _call_set_placements(app, {})

    mocks['match_svc'].set_ffa_placements.assert_not_called()
    mocks['flash_error'].assert_called_once_with('No placement data submitted.')


def test_orga_confirm_ffa_records_the_orga_as_initiator(app):
    with _patched_orga_view(app, tournament=_FFA) as mocks:
        _call_confirm_ffa(app, {})

    mocks['match_svc'].confirm_ffa_match.assert_called_once_with(
        MATCH_ID, USER_ID
    )
    mocks['flash_success'].assert_called_once_with(
        'FFA match has been confirmed.'
    )


@pytest.mark.parametrize(
    ('call', 'service_function', 'form_data'),
    [
        (
            _call_set_placements,
            'set_ffa_placements',
            {f'placement_{PARTICIPANT_A}': '1'},
        ),
        (_call_confirm_ffa, 'confirm_ffa_match', {}),
    ],
    ids=['placements', 'confirm_ffa'],
)
def test_orga_ffa_action_refuses_bracket_match(
    app, call, service_function, form_data
):
    with _patched_orga_view(app) as mocks:
        call(app, form_data)

    getattr(mocks['match_svc'], service_function).assert_not_called()
    mocks['flash_error'].assert_called_once_with(
        'Placements apply only to free-for-all matches.'
    )


def test_orga_set_ffa_placements_error_is_translated_whole(app):
    wrapper_de = 'Fehler beim Setzen der Platzierungen: %(error)s'
    catalogue = {
        'Error setting placements: %(error)s': wrapper_de,
        _SERVICE_ERROR: _SERVICE_ERROR_DE,
    }

    with (
        _patched_orga_view(
            app,
            tournament=_FFA,
            set_placements_result=Err(_SERVICE_ERROR),
        ) as mocks,
        _translating(catalogue),
    ):
        _call_set_placements(app, {f'placement_{PARTICIPANT_A}': '1'})

    _assert_flash_translated(mocks, wrapper_de)


def test_orga_confirm_ffa_error_is_translated_whole(app):
    wrapper_de = 'Fehler beim Bestätigen der FFA-Partie: %(error)s'
    catalogue = {
        'Error confirming FFA match: %(error)s': wrapper_de,
        _SERVICE_ERROR: _SERVICE_ERROR_DE,
    }

    with (
        _patched_orga_view(
            app, tournament=_FFA, confirm_ffa_result=Err(_SERVICE_ERROR)
        ) as mocks,
        _translating(catalogue),
    ):
        _call_confirm_ffa(app, {})

    _assert_flash_translated(mocks, wrapper_de)


def test_orga_resume_cannot_reopen_a_completed_tournament(app):
    """COMPLETED -> ONGOING is a valid transition, but admin-only.

    `resume` maps to ONGOING, so without the view's own status check
    an orga could reach the reopen edge through it. The template only
    offers Resume on a PAUSED tournament; the route is a plain POST.
    """
    tournament = _make_tournament(status=TournamentStatus.COMPLETED)

    with _patched_orga_view(app, tournament=tournament) as mocks:
        _call_status(app, str(TOURNAMENT_ID), 'resume')

    mocks['tournament_svc'].change_status.assert_not_called()
    mocks['flash_error'].assert_called_once_with(
        'A completed tournament can only be reopened by an administrator.'
    )


def test_orga_complete_is_refused_on_an_already_completed_tournament(app):
    tournament = _make_tournament(status=TournamentStatus.COMPLETED)

    with _patched_orga_view(app, tournament=tournament) as mocks:
        _call_status(app, str(TOURNAMENT_ID), 'complete')

    mocks['tournament_svc'].change_status.assert_not_called()
