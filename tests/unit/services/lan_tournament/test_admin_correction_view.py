"""
tests.unit.services.lan_tournament.test_admin_correction_view
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the admin match-result correction view.

The central guarantee here is that a submitted score is bound to its
contestant by KEY and never by list position: the contestant query
sorts on a ``created_at`` that is identical for every contestant of a
generated bracket, so row order is not stable between the GET that
renders the form and the POST that parses it.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from flask_babel import Babel

from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
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

_V = 'byceps.services.lan_tournament.blueprints.admin.views'


# ------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------ #


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['WTF_CSRF_ENABLED'] = False
    # The view labels the match through ``gettext``; without Babel
    # bound to the app that raises instead of falling through to the
    # msgid.
    Babel(a)
    return a


def _make_tournament(
    contestant_type: ContestantType = ContestantType.SOLO,
    game_format: GameFormat | None = GameFormat.ONE_V_ONE,
) -> MagicMock:
    t = MagicMock(spec=Tournament)
    t.id = TOURNAMENT_ID
    t.party_id = PARTY_ID_STR
    t.contestant_type = contestant_type
    # view_match reads this to skip the correction classification for
    # FFA, where the panel is not rendered at all. spec=Tournament does
    # not expose dataclass fields that carry no class-level default, so
    # it has to be set explicitly.
    t.game_format = game_format
    t.tournament_status = TournamentStatus.ONGOING
    return t


def _make_contestant(
    participant_id=None, team_id=None, score=None
) -> MagicMock:
    c = MagicMock()
    c.participant_id = participant_id
    c.team_id = team_id
    c.score = score
    return c


def _played_pair() -> list[MagicMock]:
    """A played match; fewer real contestants is a walkover."""
    return [
        _make_contestant(participant_id=PARTICIPANT_A, score=3),
        _make_contestant(participant_id=PARTICIPANT_B, score=1),
    ]


def _make_match() -> MagicMock:
    m = MagicMock()
    m.id = MATCH_ID
    m.tournament_id = TOURNAMENT_ID
    m.confirmed_by = USER_ID
    return m


@contextmanager
def _patched_correction_view(
    contestants, tournament=None, correct_result=None, translate=None
):
    """Patch the dependencies of the correction POST view.

    Pass *translate* to stand in for a real catalogue; the default
    hands every msgid back unchanged.
    """
    if tournament is None:
        tournament = _make_tournament()
    if correct_result is None:
        correct_result = Ok((CorrectionCase.NO_DOWNSTREAM, True))
    if translate is None:
        translate = lambda msg, **kw: msg  # noqa: E731

    with (
        patch(f'{_V}.gettext', side_effect=translate),
        # The score parse lives in the shared view helper.
        patch(
            'byceps.services.lan_tournament.lan_tournament_view_helpers.gettext',
            side_effect=translate,
        ),
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}._get_match_or_404') as mock_get_match,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
    ):
        mock_get_match.return_value = _make_match()
        mock_get_tournament.return_value = tournament
        mock_match_svc.get_contestants_for_match.return_value = contestants
        mock_match_svc.correct_match_result.return_value = correct_result
        yield {
            'flash_error': mock_flash_error,
            'flash_success': mock_flash_success,
            'redirect_to': mock_redirect_to,
            'match_svc': mock_match_svc,
        }


def _call_correct(app, form_data):
    """Call the raw correct_match_result POST view."""
    from byceps.services.lan_tournament.blueprints.admin import views

    raw_fn = views.correct_match_result.__wrapped__

    with app.test_request_context('/', method='POST', data=form_data):
        with patch(f'{_V}.g') as mock_g:
            mock_g.user.id = USER_ID
            return raw_fn(MATCH_ID_STR)


def _submitted_scores(mocks):
    """Return the corrected_scores kwarg the service received."""
    mocks['match_svc'].correct_match_result.assert_called_once()
    return mocks['match_svc'].correct_match_result.call_args.kwargs[
        'corrected_scores'
    ]


# ------------------------------------------------------------------ #
# key binding — the HIGH defect
# ------------------------------------------------------------------ #


def test_correction_binds_score_to_contestant_key_not_position(app):
    """A score posted under a contestant key reaches that contestant."""
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A, score=1),
        _make_contestant(participant_id=PARTICIPANT_B, score=0),
    ]
    form_data = {
        'reason': 'scorekeeper entered the wrong side',
        f'corrected_score_{PARTICIPANT_A}': '3',
        f'corrected_score_{PARTICIPANT_B}': '1',
    }

    with _patched_correction_view(contestants) as mocks:
        _call_correct(app, form_data)

    assert _submitted_scores(mocks) == {
        PARTICIPANT_A: 3,
        PARTICIPANT_B: 1,
    }


def test_correction_binding_survives_reversed_contestant_order(app):
    """The same form yields the same map when row order flips.

    This is the regression guard. ``get_contestants_for_match`` orders
    by a tied ``created_at``, so the database may return the two rows
    in either order. A positional home/away binding would invert the
    result here; a keyed binding must not.
    """
    form_data = {
        'reason': 'scorekeeper entered the wrong side',
        f'corrected_score_{PARTICIPANT_A}': '3',
        f'corrected_score_{PARTICIPANT_B}': '1',
    }

    forward = [
        _make_contestant(participant_id=PARTICIPANT_A, score=1),
        _make_contestant(participant_id=PARTICIPANT_B, score=0),
    ]
    with _patched_correction_view(forward) as mocks:
        _call_correct(app, form_data)
        forward_scores = _submitted_scores(mocks)

    reversed_ = [
        _make_contestant(participant_id=PARTICIPANT_B, score=0),
        _make_contestant(participant_id=PARTICIPANT_A, score=1),
    ]
    with _patched_correction_view(reversed_) as mocks:
        _call_correct(app, form_data)
        reversed_scores = _submitted_scores(mocks)

    assert forward_scores == reversed_scores
    assert forward_scores == {PARTICIPANT_A: 3, PARTICIPANT_B: 1}


def test_correction_binds_team_keys_for_team_tournaments(app):
    """A team tournament casts the key to TournamentTeamID."""
    team_a = TournamentTeamID(generate_uuid())
    team_b = TournamentTeamID(generate_uuid())
    contestants = [
        _make_contestant(team_id=team_a, score=2),
        _make_contestant(team_id=team_b, score=2),
    ]
    form_data = {
        'reason': 'recount',
        f'corrected_score_{team_a}': '5',
        f'corrected_score_{team_b}': '2',
    }
    tournament = _make_tournament(ContestantType.TEAM)

    with _patched_correction_view(contestants, tournament) as mocks:
        _call_correct(app, form_data)

    assert _submitted_scores(mocks) == {team_a: 5, team_b: 2}


# ------------------------------------------------------------------ #
# partial / blank / malformed input
# ------------------------------------------------------------------ #


def test_correction_rejects_partial_scores(app):
    """One score filled and one blank must not reach the service."""
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A),
        _make_contestant(participant_id=PARTICIPANT_B),
    ]
    form_data = {
        'reason': 'partial',
        f'corrected_score_{PARTICIPANT_A}': '3',
        f'corrected_score_{PARTICIPANT_B}': '',
    }

    with _patched_correction_view(contestants) as mocks:
        _call_correct(app, form_data)

    mocks['match_svc'].correct_match_result.assert_not_called()
    mocks['flash_error'].assert_called_once()


def test_correction_all_blank_scores_retracts_only(app):
    """No scores posted retracts the result without re-entering one."""
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A),
        _make_contestant(participant_id=PARTICIPANT_B),
    ]
    form_data = {'reason': 'result disputed, pending review'}

    with _patched_correction_view(
        contestants, correct_result=Ok((CorrectionCase.NO_DOWNSTREAM, False))
    ) as mocks:
        _call_correct(app, form_data)

    assert _submitted_scores(mocks) is None


def test_correction_rejects_non_integer_score(app):
    """A non-numeric score is refused before anything destructive runs."""
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A),
        _make_contestant(participant_id=PARTICIPANT_B),
    ]
    form_data = {
        'reason': 'typo',
        f'corrected_score_{PARTICIPANT_A}': 'abc',
        f'corrected_score_{PARTICIPANT_B}': '1',
    }

    with _patched_correction_view(contestants) as mocks:
        _call_correct(app, form_data)

    mocks['match_svc'].correct_match_result.assert_not_called()
    mocks['flash_error'].assert_called_once_with('Invalid score value.')


def test_correction_skips_defwin_slot(app):
    """A DEFWIN slot carries no key, so it is neither named nor required."""
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A),
        _make_contestant(),  # DEFWIN: no participant, no team
    ]
    form_data = {
        'reason': 'bye corrected',
        f'corrected_score_{PARTICIPANT_A}': '1',
    }

    with _patched_correction_view(contestants) as mocks:
        _call_correct(app, form_data)

    scores = _submitted_scores(mocks)
    assert scores == {PARTICIPANT_A: 1}
    assert None not in scores


def test_correction_reason_is_required(app):
    """A blank reason fails form validation before any service call."""
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A),
        _make_contestant(participant_id=PARTICIPANT_B),
    ]

    with _patched_correction_view(contestants) as mocks:
        _call_correct(app, {'reason': ''})

    mocks['match_svc'].correct_match_result.assert_not_called()
    mocks['flash_error'].assert_called_once()


def test_correction_reason_over_max_length_rejected(app):
    """A reason beyond the 2000-character bound is refused."""
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A),
        _make_contestant(participant_id=PARTICIPANT_B),
    ]
    form_data = {
        'reason': 'x' * 2001,
        f'corrected_score_{PARTICIPANT_A}': '1',
        f'corrected_score_{PARTICIPANT_B}': '0',
    }

    with _patched_correction_view(contestants) as mocks:
        _call_correct(app, form_data)

    mocks['match_svc'].correct_match_result.assert_not_called()
    mocks['flash_error'].assert_called_once()


def test_correction_reason_at_max_length_accepted(app):
    """Exactly 2000 characters is within the bound."""
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A),
        _make_contestant(participant_id=PARTICIPANT_B),
    ]
    form_data = {
        'reason': 'x' * 2000,
        f'corrected_score_{PARTICIPANT_A}': '1',
        f'corrected_score_{PARTICIPANT_B}': '0',
    }

    with _patched_correction_view(contestants) as mocks:
        _call_correct(app, form_data)

    mocks['match_svc'].correct_match_result.assert_called_once()


# ------------------------------------------------------------------ #
# error surfacing
# ------------------------------------------------------------------ #


def test_correction_surfaces_service_error(app):
    """A service Err is flashed rather than reported as success."""
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A),
        _make_contestant(participant_id=PARTICIPANT_B),
    ]
    form_data = {
        'reason': 'downstream already played',
        f'corrected_score_{PARTICIPANT_A}': '1',
        f'corrected_score_{PARTICIPANT_B}': '0',
    }

    with _patched_correction_view(
        contestants,
        correct_result=Err('explicit acknowledgement is required.'),
    ) as mocks:
        _call_correct(app, form_data)

    mocks['flash_success'].assert_not_called()
    mocks['flash_error'].assert_called_once()


def test_correction_error_is_translated_whole(app):
    """The flash must not be half translated.

    The service reports its failures as English msgids. Interpolating
    one straight into the translated wrapper left an admin who left
    the acknowledgement unticked reading a German sentence with an
    English tail.
    """
    service_error = (
        'Downstream matches already started or completed; '
        'explicit acknowledgement is required.'
    )
    catalogue = {
        'Error correcting match result: %(error)s Nothing was changed; '
        'the original result remains confirmed.': (
            'Fehler bei der Korrektur des Partieergebnisses: %(error)s '
            'Es wurde nichts geändert; das ursprüngliche Ergebnis '
            'bleibt bestätigt.'
        ),
        service_error: (
            'Folgepartien wurden bereits begonnen oder abgeschlossen; '
            'eine ausdrückliche Bestätigung ist erforderlich.'
        ),
    }

    def translate(msg, **kw):
        translated = catalogue.get(msg, msg)
        return translated % kw if kw else translated

    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A),
        _make_contestant(participant_id=PARTICIPANT_B),
    ]
    form_data = {
        'reason': 'downstream already played',
        f'corrected_score_{PARTICIPANT_A}': '1',
        f'corrected_score_{PARTICIPANT_B}': '0',
    }

    with _patched_correction_view(
        contestants,
        correct_result=Err(service_error),
        translate=translate,
    ) as mocks:
        _call_correct(app, form_data)

    (flashed,) = mocks['flash_error'].call_args.args
    assert flashed.startswith('Fehler bei der Korrektur')
    assert 'Folgepartien wurden bereits begonnen' in flashed
    assert 'acknowledgement' not in flashed
    # The reassurance moved out of the service message and into this
    # wrapper msgid, so it must be translated along with it rather
    # than surviving as an English tail.
    assert 'Es wurde nichts geändert' in flashed
    assert 'Nothing was changed' not in flashed


# ------------------------------------------------------------------ #
# bounded queries on the panel (goal G3)
# ------------------------------------------------------------------ #


def _call_view_match(app):
    from byceps.services.lan_tournament.blueprints.admin import views

    raw_fn = views.view_match.__wrapped__.__wrapped__

    mock_g = MagicMock()
    mock_g.user.has_permission.return_value = True
    mock_g.user.id = USER_ID

    with app.test_request_context('/'):
        with patch(f'{_V}.g', new=mock_g):
            return raw_fn(MATCH_ID_STR)


def test_view_match_query_count_independent_of_downstream_count(app):
    """The panel fetches downstream matches in ONE batched call.

    Previously this issued one get_match per affected bracket node on
    every load of a confirmed-match admin page.
    """
    downstream_ids = [
        TournamentMatchID(generate_uuid()) for _ in range(6)
    ]

    def _fake_match(mid):
        m = MagicMock()
        m.id = mid
        return m

    with (
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(f'{_V}.build_contestant_name_lookups') as mock_names,
        patch(f'{_V}.build_hover_lookups') as mock_hover,
        patch(f'{_V}._get_match_or_404') as mock_get_match,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
    ):
        mock_get_match.return_value = _make_match()
        mock_get_tournament.return_value = _make_tournament()
        mock_party_svc.get_party.return_value = MagicMock()
        mock_names.return_value = ({}, {})
        mock_hover.return_value = ({}, {})
        mock_user_svc.get_users_indexed_by_id.return_value = {}
        mock_match_svc.get_contestants_for_match.return_value = (
            _played_pair()
        )
        mock_match_svc.get_comments_from_match.return_value = []
        mock_match_svc.classify_result_correction.return_value = Ok(
            (CorrectionCase.CONFIRMED_DOWNSTREAM, downstream_ids)
        )
        mock_match_svc.get_matches_by_ids.return_value = [
            _fake_match(mid) for mid in downstream_ids
        ]
        mock_match_svc.get_contestants_for_matches.return_value = {}

        context = _call_view_match(app)

    assert mock_match_svc.get_matches_by_ids.call_count == 1
    assert mock_match_svc.get_contestants_for_matches.call_count == 1
    mock_match_svc.get_match.assert_not_called()
    # One call for THIS match; the affected ones go through the
    # batched fetch above, not one call each.
    assert mock_match_svc.get_contestants_for_match.call_count == 1

    # BFS order from the service must survive the batched fetch.
    assert [
        row.match.id for row in context['downstream_impact']
    ] == downstream_ids


def test_view_match_preserves_bfs_order_when_fetch_returns_shuffled(app):
    """The batched fetch returns arbitrary order; the view re-sorts it."""
    downstream_ids = [
        TournamentMatchID(generate_uuid()) for _ in range(4)
    ]

    def _fake_match(mid):
        m = MagicMock()
        m.id = mid
        return m

    with (
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(f'{_V}.build_contestant_name_lookups') as mock_names,
        patch(f'{_V}.build_hover_lookups') as mock_hover,
        patch(f'{_V}._get_match_or_404') as mock_get_match,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
    ):
        mock_get_match.return_value = _make_match()
        mock_get_tournament.return_value = _make_tournament()
        mock_party_svc.get_party.return_value = MagicMock()
        mock_names.return_value = ({}, {})
        mock_hover.return_value = ({}, {})
        mock_user_svc.get_users_indexed_by_id.return_value = {}
        mock_match_svc.get_contestants_for_match.return_value = (
            _played_pair()
        )
        mock_match_svc.get_comments_from_match.return_value = []
        mock_match_svc.classify_result_correction.return_value = Ok(
            (CorrectionCase.UNCONFIRMED_DOWNSTREAM, downstream_ids)
        )
        # Deliberately reversed, as an IN(...) query may return.
        mock_match_svc.get_matches_by_ids.return_value = [
            _fake_match(mid) for mid in reversed(downstream_ids)
        ]
        mock_match_svc.get_contestants_for_matches.return_value = {}

        context = _call_view_match(app)

    assert [
        row.match.id for row in context['downstream_impact']
    ] == downstream_ids


# ------------------------------------------------------------------ #
# the panel's preview work is skipped where the panel is not rendered
# ------------------------------------------------------------------ #


def _call_view_match_with_tournament(
    app, tournament, contestants=None, ffa_advanced=False
):
    from byceps.services.lan_tournament.blueprints.admin import views

    raw_fn = views.view_match.__wrapped__.__wrapped__

    mock_g = MagicMock()
    mock_g.user.has_permission.return_value = True
    mock_g.user.id = USER_ID

    with (
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(f'{_V}.build_contestant_name_lookups') as mock_names,
        patch(f'{_V}.build_hover_lookups') as mock_hover,
        patch(f'{_V}._get_match_or_404') as mock_get_match,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
    ):
        mock_get_match.return_value = _make_match()
        mock_get_tournament.return_value = tournament
        mock_party_svc.get_party.return_value = MagicMock()
        mock_names.return_value = ({}, {})
        mock_hover.return_value = ({}, {})
        mock_user_svc.get_users_indexed_by_id.return_value = {}
        mock_match_svc.get_contestants_for_match.return_value = (
            contestants if contestants is not None else _played_pair()
        )
        mock_match_svc.get_comments_from_match.return_value = []
        mock_match_svc.classify_result_correction.return_value = Ok(
            (CorrectionCase.NO_DOWNSTREAM, [])
        )
        mock_match_svc.get_matches_by_ids.return_value = []
        mock_match_svc.get_contestants_for_matches.return_value = {}
        mock_match_svc.ffa_round_already_advanced.return_value = ffa_advanced

        with app.test_request_context('/'):
            with patch(f'{_V}.g', new=mock_g):
                context = raw_fn(MATCH_ID_STR)

        return context, mock_match_svc


def test_view_match_skips_correction_classification_for_ffa(app):
    """view_match.html renders the correction panel only in its
    non-FFA branch, and an FFA match routes nothing downstream, so
    classifying one is two queries and a whole-bracket load per page
    view for a result nothing draws."""
    tournament = _make_tournament(game_format=GameFormat.FREE_FOR_ALL)

    context, mock_match_svc = _call_view_match_with_tournament(
        app, tournament
    )

    mock_match_svc.classify_result_correction.assert_not_called()
    assert context['correction_case'] is None
    assert context['downstream_impact'] == []


def test_view_match_flags_an_advanced_ffa_group(app):
    """The page must not offer an unconfirm the service refuses."""
    tournament = _make_tournament(game_format=GameFormat.FREE_FOR_ALL)

    context, mock_match_svc = _call_view_match_with_tournament(
        app, tournament, ffa_advanced=True
    )

    assert context['ffa_result_consumed'] is True
    (call,) = mock_match_svc.ffa_round_already_advanced.call_args_list
    assert call.args[1] is tournament


def test_view_match_skips_the_ffa_advance_check_for_brackets(app):
    tournament = _make_tournament(game_format=GameFormat.ONE_V_ONE)

    context, mock_match_svc = _call_view_match_with_tournament(
        app, tournament
    )

    assert context['ffa_result_consumed'] is False
    mock_match_svc.ffa_round_already_advanced.assert_not_called()


def test_view_match_offers_no_correction_for_a_walkover(app):
    """No form and no classification for a walkover."""
    tournament = _make_tournament(game_format=GameFormat.ONE_V_ONE)

    context, mock_match_svc = _call_view_match_with_tournament(
        app,
        tournament,
        contestants=[_make_contestant(participant_id=PARTICIPANT_A)],
    )

    assert context['is_walkover'] is True
    assert context['correction_case'] is None
    mock_match_svc.classify_result_correction.assert_not_called()


def test_view_match_offers_correction_for_a_played_match(app):
    """The walkover skip must not swallow the panel of a real match."""
    tournament = _make_tournament(game_format=GameFormat.ONE_V_ONE)

    context, _mock_match_svc = _call_view_match_with_tournament(
        app, tournament
    )

    assert context['is_walkover'] is False


def test_view_match_still_classifies_for_non_ffa(app):
    """The FFA skip must not swallow the panel it was added beside."""
    tournament = _make_tournament(game_format=GameFormat.ONE_V_ONE)

    context, mock_match_svc = _call_view_match_with_tournament(
        app, tournament
    )

    mock_match_svc.classify_result_correction.assert_called_once()
    assert context['correction_case'] is CorrectionCase.NO_DOWNSTREAM


# ------------------------------------------------------------------ #
# the "this clears the tournament winner" banner
# ------------------------------------------------------------------ #


def _call_view_match_for(
    app,
    *,
    bracket,
    next_match_id,
    elimination_mode,
    tournament_status=TournamentStatus.COMPLETED,
    contestants=None,
):
    """Run view_match over a match of a given bracket position."""
    from byceps.services.lan_tournament.blueprints.admin import views

    raw_fn = views.view_match.__wrapped__.__wrapped__

    tournament = _make_tournament()
    tournament.elimination_mode = elimination_mode
    tournament.tournament_status = tournament_status

    match = _make_match()
    match.bracket = bracket
    match.next_match_id = next_match_id

    mock_g = MagicMock()
    mock_g.user.has_permission.return_value = True
    mock_g.user.id = USER_ID

    with (
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(f'{_V}.build_contestant_name_lookups') as mock_names,
        patch(f'{_V}.build_hover_lookups') as mock_hover,
        patch(f'{_V}._get_match_or_404') as mock_get_match,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
    ):
        mock_get_match.return_value = match
        mock_get_tournament.return_value = tournament
        mock_party_svc.get_party.return_value = MagicMock()
        mock_names.return_value = ({}, {})
        mock_hover.return_value = ({}, {})
        mock_user_svc.get_users_indexed_by_id.return_value = {}
        mock_match_svc.get_contestants_for_match.return_value = (
            contestants if contestants is not None else _played_pair()
        )
        mock_match_svc.get_comments_from_match.return_value = []
        mock_match_svc.classify_result_correction.return_value = Ok(
            (CorrectionCase.NO_DOWNSTREAM, [])
        )
        mock_match_svc.get_matches_by_ids.return_value = []
        mock_match_svc.get_contestants_for_matches.return_value = {}

        with app.test_request_context('/'):
            with patch(f'{_V}.g', new=mock_g):
                return raw_fn(MATCH_ID_STR)


@pytest.mark.parametrize(
    'elimination_mode',
    [EliminationMode.SINGLE_ELIMINATION, EliminationMode.DOUBLE_ELIMINATION],
)
def test_banner_shown_for_the_real_terminal_match(app, elimination_mode):
    """The final decides the tournament, so retracting it clears it."""
    context = _call_view_match_for(
        app,
        bracket=Bracket.WINNERS,
        next_match_id=None,
        elimination_mode=elimination_mode,
    )

    assert context['correction_clears_winner'] is True


@pytest.mark.parametrize(
    'tournament_status',
    [
        TournamentStatus.ONGOING,
        TournamentStatus.PAUSED,
        TournamentStatus.CANCELLED,
    ],
)
def test_banner_not_shown_when_the_tournament_is_not_completed(
    app, tournament_status
):
    """Only a completed tournament has a completion to revert."""
    context = _call_view_match_for(
        app,
        bracket=Bracket.WINNERS,
        next_match_id=None,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=tournament_status,
    )

    assert context['correction_clears_winner'] is False


def test_banner_not_shown_for_the_third_place_match(app):
    """P3 carries next_match_id=None too, but decides nothing.

    It ranks the two semifinal losers; the champion comes out of the
    final. Claiming otherwise is not merely a cosmetic banner bug --
    the view used the very condition the service used, and the
    service really did clear the winner. Both were fixed together,
    so both are pinned here.
    """
    context = _call_view_match_for(
        app,
        bracket=Bracket.THIRD_PLACE,
        next_match_id=None,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    assert context['correction_clears_winner'] is False


def test_banner_not_shown_for_a_non_terminal_match(app):
    """Something follows it, so the winner is not at stake."""
    context = _call_view_match_for(
        app,
        bracket=Bracket.WINNERS,
        next_match_id=TournamentMatchID(generate_uuid()),
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    assert context['correction_clears_winner'] is False


def test_banner_not_shown_for_round_robin(app):
    """Round robin has no terminal match and no auto-complete."""
    context = _call_view_match_for(
        app,
        bracket=None,
        next_match_id=None,
        elimination_mode=EliminationMode.ROUND_ROBIN,
    )

    assert context['correction_clears_winner'] is False


# ------------------------------------------------------------------ #
# the ID the view hands to the service layer
# ------------------------------------------------------------------ #


def test_get_match_or_404_passes_a_real_uuid_to_the_service():
    """TournamentMatchID is a NewType -- a no-op at runtime -- and the
    routes use Flask's default string converter, so without an explicit
    parse every ID reaching the service layer was a str. Queries still
    worked (SQLAlchemy coerces on bind), but the UUID-keyed lookups in
    _lock_reachable_matches and classify_result_correction did not."""
    from uuid import UUID

    from byceps.services.lan_tournament.blueprints.admin import views

    with patch(f'{_V}.tournament_match_service') as mock_match_svc:
        mock_match_svc.find_match.return_value = _make_match()

        views._get_match_or_404(MATCH_ID_STR)

    (passed_id,) = mock_match_svc.find_match.call_args.args
    assert isinstance(passed_id, UUID)
    assert passed_id == MATCH_ID


def test_get_match_or_404_rejects_a_malformed_id_with_404():
    """A malformed ID used to reach the driver as a str and raise a
    DataError -- a 500 with a stack trace on a guessable URL."""
    from werkzeug.exceptions import NotFound

    from byceps.services.lan_tournament.blueprints.admin import views

    with patch(f'{_V}.tournament_match_service') as mock_match_svc:
        with pytest.raises(NotFound):
            views._get_match_or_404('not-a-uuid')

        mock_match_svc.find_match.assert_not_called()


def test_get_tournament_or_404_rejects_a_malformed_id_with_404():
    """Same defect as _get_match_or_404's, on the 29 ``<tournament_id>``
    admin routes: find_tournament() is a db.session.get() that hands an
    unparsed str straight to psycopg, which raises DataError ("invalid
    input syntax for type uuid") -- a 500 on a guessable URL, and a
    session left in a failed transaction so every later statement in
    the request aborts too. The site blueprint's own helper already
    parses; the admin one did not."""
    from uuid import UUID

    from werkzeug.exceptions import NotFound

    from byceps.services.lan_tournament.blueprints.admin import views

    with patch(f'{_V}.tournament_service') as mock_tournament_svc:
        with pytest.raises(NotFound):
            views._get_tournament_or_404('not-a-uuid')

        mock_tournament_svc.find_tournament.assert_not_called()

        mock_tournament_svc.find_tournament.return_value = _make_tournament()
        views._get_tournament_or_404(str(TOURNAMENT_ID))

    (passed_id,) = mock_tournament_svc.find_tournament.call_args.args
    assert isinstance(passed_id, UUID)
    assert passed_id == TOURNAMENT_ID


def test_get_team_or_404_rejects_a_malformed_id_with_404():
    """Same parse-before-query reason as the tournament helper above."""
    from werkzeug.exceptions import NotFound

    from byceps.services.lan_tournament.blueprints.admin import views

    with patch(f'{_V}.tournament_team_service') as mock_team_svc:
        with pytest.raises(NotFound):
            views._get_team_or_404('not-a-uuid')

        mock_team_svc.find_team.assert_not_called()


def test_delete_match_comment_rejects_a_malformed_comment_id_with_404():
    """Both IDs on this route came off the URL unparsed. A bare UUID()
    on a malformed one raised ValueError out of the view -- a 500 on a
    guessable URL -- instead of a 404."""
    from werkzeug.exceptions import NotFound

    from byceps.services.lan_tournament.blueprints.admin import views

    raw_fn = views.delete_match_comment.__wrapped__

    with (
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}._get_match_or_404') as mock_get_match,
    ):
        mock_get_match.return_value = _make_match()

        with pytest.raises(NotFound):
            raw_fn(MATCH_ID_STR, 'not-a-uuid')

        mock_match_svc.delete_comment.assert_not_called()


def test_delete_match_comment_passes_real_uuids_to_the_service():
    from uuid import UUID

    from byceps.services.lan_tournament.blueprints.admin import views

    raw_fn = views.delete_match_comment.__wrapped__
    comment_id = generate_uuid()

    with (
        patch(f'{_V}.gettext', side_effect=lambda m, **kw: m),
        patch(f'{_V}.flash_success'),
        patch(f'{_V}.flash_error'),
        patch(f'{_V}.redirect_to'),
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}._get_match_or_404') as mock_get_match,
    ):
        mock_get_match.return_value = _make_match()
        mock_match_svc.delete_comment.return_value = Ok(None)

        raw_fn(MATCH_ID_STR, str(comment_id))

    passed_comment_id, passed_match_id = (
        mock_match_svc.delete_comment.call_args.args
    )
    assert isinstance(passed_comment_id, UUID)
    assert isinstance(passed_match_id, UUID)
    assert passed_comment_id == comment_id
    assert passed_match_id == MATCH_ID


# ------------------------------------------------------------------ #
# the correction route is 1v1-only, like the panel that posts to it
# ------------------------------------------------------------------ #


def test_correct_match_result_refuses_an_ffa_match(app):
    """The whole cascade is built on next_match_id, which FFA does not
    use, so a correction posted at an FFA match would degrade into a
    plain unconfirm carrying a correction's audit entries."""
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A, score=1),
        _make_contestant(participant_id=PARTICIPANT_B, score=0),
    ]
    ffa_tournament = _make_tournament(game_format=GameFormat.FREE_FOR_ALL)
    form_data = {
        'reason': 'placements were entered wrong',
        f'corrected_score_{PARTICIPANT_A}': '3',
        f'corrected_score_{PARTICIPANT_B}': '1',
    }

    with _patched_correction_view(
        contestants, tournament=ffa_tournament
    ) as mocks:
        _call_correct(app, form_data)

    mocks['match_svc'].correct_match_result.assert_not_called()
    mocks['flash_error'].assert_called_once()
    (flashed,) = mocks['flash_error'].call_args.args
    assert 'Free-for-all' in flashed


def test_correct_match_result_still_runs_for_a_1v1_match(app):
    """The FFA guard must not block the path it was added beside."""
    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A, score=1),
        _make_contestant(participant_id=PARTICIPANT_B, score=0),
    ]
    form_data = {
        'reason': 'scorekeeper entered the wrong side',
        f'corrected_score_{PARTICIPANT_A}': '3',
        f'corrected_score_{PARTICIPANT_B}': '1',
    }

    with _patched_correction_view(
        contestants,
        tournament=_make_tournament(game_format=GameFormat.ONE_V_ONE),
    ) as mocks:
        _call_correct(app, form_data)

    mocks['match_svc'].correct_match_result.assert_called_once()


# ------------------------------------------------------------------ #
# the sibling flashes must be translated whole too
# ------------------------------------------------------------------ #


def _translator(catalogue):
    def translate(msg, **kw):
        translated = catalogue.get(msg, msg)
        return translated % kw if kw else translated

    return translate


@contextmanager
def _patched_match_action_view(service_result, translate, tournament=None):
    """Patch the confirm_with_scores / unconfirm POST views.

    Both take the same shape as the correction view: look the match
    up, look its tournament up, call one service function, flash the
    outcome.
    """
    if tournament is None:
        tournament = _make_tournament()

    with (
        patch(f'{_V}.gettext', side_effect=translate),
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_success'),
        patch(f'{_V}.redirect_to'),
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}._get_match_or_404') as mock_get_match,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
    ):
        mock_get_match.return_value = _make_match()
        mock_get_tournament.return_value = tournament
        mock_match_svc.admin_set_and_confirm_match.return_value = (
            service_result
        )
        mock_match_svc.unconfirm_match.return_value = service_result
        yield {
            'flash_error': mock_flash_error,
            'match_svc': mock_match_svc,
        }


def test_confirm_with_scores_error_is_translated_whole(app):
    """``confirm_with_scores`` must not flash a half-German sentence.

    ``_validate_match_scores`` routes two English msgids through this
    view that did not previously reach it. Interpolating one straight
    into the translated wrapper reproduces the exact defect the
    correction and status-change flashes were fixed for.
    """
    service_error = 'Cannot confirm match with less than 2 contestants.'
    translate = _translator(
        {
            'Error confirming match: %(error)s': (
                'Fehler beim Bestätigen der Partie: %(error)s'
            ),
            service_error: (
                'Eine Partie mit weniger als 2 Teilnehmern kann nicht '
                'bestätigt werden.'
            ),
        }
    )

    contestants = [
        _make_contestant(participant_id=PARTICIPANT_A),
        _make_contestant(participant_id=PARTICIPANT_B),
    ]
    form_data = {
        f'score_{PARTICIPANT_A}': '3',
        f'score_{PARTICIPANT_B}': '1',
    }

    from byceps.services.lan_tournament.blueprints.admin import views

    raw_fn = views.confirm_match_with_scores.__wrapped__

    with _patched_match_action_view(
        Err(service_error), translate
    ) as mocks:
        mocks['match_svc'].get_contestants_for_match.return_value = (
            contestants
        )
        with app.test_request_context('/', method='POST', data=form_data):
            with patch(f'{_V}.g') as mock_g:
                mock_g.user.id = USER_ID
                raw_fn(MATCH_ID_STR)

    (flashed,) = mocks['flash_error'].call_args.args
    assert flashed.startswith('Fehler beim Bestätigen der Partie')
    assert 'weniger als 2 Teilnehmern' in flashed
    assert 'Cannot confirm match' not in flashed


def test_unconfirm_error_is_translated_whole(app):
    """``unconfirm_match`` must not flash a half-German sentence."""
    service_error = 'Match is not confirmed.'
    translate = _translator(
        {
            'Error unconfirming match: %(error)s': (
                'Fehler beim Zurückziehen der Partie: %(error)s'
            ),
            service_error: 'Die Partie ist nicht bestätigt.',
        }
    )

    from byceps.services.lan_tournament.blueprints.admin import views

    raw_fn = views.unconfirm_match.__wrapped__

    # The route serves FFA only; bracket matches are sent to the
    # correction panel before the service is ever called.
    ffa = _make_tournament(game_format=GameFormat.FREE_FOR_ALL)

    with _patched_match_action_view(
        Err(service_error), translate, tournament=ffa
    ) as mocks:
        with app.test_request_context(
            '/', method='POST', data={'reason': 'wrong placements'}
        ):
            with patch(f'{_V}.g') as mock_g:
                mock_g.user.id = USER_ID
                raw_fn(MATCH_ID_STR)

    (flashed,) = mocks['flash_error'].call_args.args
    assert flashed.startswith('Fehler beim Zurückziehen der Partie')
    assert 'Die Partie ist nicht bestätigt' in flashed
    assert 'Match is not confirmed' not in flashed


def test_set_ffa_placements_error_is_translated_whole(app):
    """``set_ffa_placements`` must not flash a half-German sentence."""
    service_error = 'Cannot modify placements of a confirmed match.'
    translate = _translator(
        {
            'Error setting placements: %(error)s': (
                'Fehler beim Setzen der Platzierungen: %(error)s'
            ),
            service_error: (
                'Platzierungen einer bestätigten Partie können nicht '
                'geändert werden.'
            ),
        }
    )

    from byceps.services.lan_tournament.blueprints.admin import views

    raw_fn = views.set_ffa_placements_action.__wrapped__

    with _patched_match_action_view(Err(service_error), translate) as mocks:
        mocks['match_svc'].set_ffa_placements.return_value = Err(service_error)
        with app.test_request_context(
            '/', method='POST', data={f'placement_{PARTICIPANT_A}': '1'}
        ):
            raw_fn(MATCH_ID_STR)

    (flashed,) = mocks['flash_error'].call_args.args
    assert flashed.startswith('Fehler beim Setzen der Platzierungen')
    assert 'bestätigten Partie' in flashed
    assert 'Cannot modify placements' not in flashed


# ------------------------------------------------------------------ #
# the acknowledgement names the matches it was given for
# ------------------------------------------------------------------ #


def _acknowledged_ids(mocks):
    mocks['match_svc'].correct_match_result.assert_called_once()
    return mocks['match_svc'].correct_match_result.call_args.kwargs[
        'acknowledged_match_ids'
    ]


def test_correction_passes_the_acknowledged_match_ids(app):
    first = TournamentMatchID(generate_uuid())
    second = TournamentMatchID(generate_uuid())
    form_data = {
        'reason': 'disputed',
        'ack_critical': 'y',
        'ack_match_ids': f'{first},{second}',
    }

    with _patched_correction_view(_played_pair()) as mocks:
        _call_correct(app, form_data)

    assert _acknowledged_ids(mocks) == [first, second]


@pytest.mark.parametrize('raw', [None, '', 'not-a-uuid'])
def test_correction_without_valid_acknowledged_ids_acknowledges_nothing(
    app, raw
):
    form_data = {'reason': 'disputed', 'ack_critical': 'y'}
    if raw is not None:
        form_data['ack_match_ids'] = raw

    with _patched_correction_view(_played_pair()) as mocks:
        _call_correct(app, form_data)

    assert _acknowledged_ids(mocks) == []


def test_view_match_lists_the_matches_the_acknowledgement_covers(app):
    confirmed = MagicMock(id=TournamentMatchID(generate_uuid()))
    confirmed.confirmed_by = USER_ID
    pending = MagicMock(id=TournamentMatchID(generate_uuid()))
    pending.confirmed_by = None

    from byceps.services.lan_tournament.blueprints.admin import views

    raw_fn = views.view_match.__wrapped__.__wrapped__
    mock_g = MagicMock()
    mock_g.user.has_permission.return_value = True

    with (
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}.party_service'),
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(f'{_V}.build_contestant_name_lookups') as mock_names,
        patch(f'{_V}.build_hover_lookups') as mock_hover,
        patch(f'{_V}._get_match_or_404') as mock_get_match,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
    ):
        mock_get_match.return_value = _make_match()
        mock_get_tournament.return_value = _make_tournament()
        mock_names.return_value = ({}, {})
        mock_hover.return_value = ({}, {})
        mock_user_svc.get_users_indexed_by_id.return_value = {}
        mock_match_svc.get_contestants_for_match.return_value = _played_pair()
        mock_match_svc.get_comments_from_match.return_value = []
        mock_match_svc.classify_result_correction.return_value = Ok(
            (CorrectionCase.CONFIRMED_DOWNSTREAM, [confirmed.id, pending.id])
        )
        mock_match_svc.get_matches_by_ids.return_value = [confirmed, pending]
        mock_match_svc.get_contestants_for_matches.return_value = {}

        with app.test_request_context('/'):
            with patch(f'{_V}.g', new=mock_g):
                context = raw_fn(MATCH_ID_STR)

    assert context['ack_match_ids'] == [str(confirmed.id)]
