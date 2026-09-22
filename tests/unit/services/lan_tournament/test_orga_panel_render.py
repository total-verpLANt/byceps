"""
tests.unit.services.lan_tournament.test_orga_panel_render
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import pathlib
from types import SimpleNamespace

from jinja2 import DictLoader, Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
)


_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament/orga_actions.html'
)


@pytest.fixture(scope='module')
def env():
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'orga': _TEMPLATE.read_text(),
                'macros/icons.html': (
                    '{% macro render_icon(name) %}{% endmacro %}'
                ),
                'macros/lan_tournament.html': (
                    '{% macro render_match_ref(m) %}{{ m.id }}{% endmacro %}'
                ),
            }
        ),
    )
    e.globals['_'] = lambda s, **kw: s
    e.globals['url_for'] = lambda endpoint, **k: endpoint
    return e


def _solo(key, score=None, placement=None):
    return SimpleNamespace(
        team_id=None, participant_id=key, score=score, placement=placement
    )


def _render_match_actions(
    env,
    *,
    case,
    downstream=(),
    confirmed='orga',
    ack_match_ids=(),
    is_ffa=False,
    is_walkover=False,
    ffa_result_consumed=False,
    results_editable=True,
    contestants=None,
):
    if contestants is None:
        contestants = [_solo('u1', 3), _solo('u2', 1)]
    tmpl = env.get_template('orga')
    return tmpl.module.render_orga_match_actions(
        match=SimpleNamespace(id='m0', confirmed_by=confirmed),
        tournament=SimpleNamespace(
            id='t0', contestant_type=SimpleNamespace(name='SOLO')
        ),
        contestants=contestants,
        teams_by_id={},
        participants_by_id={'u1': SimpleNamespace(screen_name='PlayerOne')},
        may_administrate=True,
        max_match_score=999_999_999,
        correction_case=case,
        affected_downstream_matches=list(downstream),
        ack_match_ids=list(ack_match_ids),
        is_ffa=is_ffa,
        is_walkover=is_walkover,
        ffa_result_consumed=ffa_result_consumed,
        results_editable=results_editable,
    )


@pytest.mark.parametrize(
    ('case', 'expected'),
    [
        (CorrectionCase.NO_DOWNSTREAM, 'No downstream matches are affected'),
        (
            CorrectionCase.UNCONFIRMED_DOWNSTREAM,
            'are not yet confirmed',
        ),
        (
            CorrectionCase.CONFIRMED_DOWNSTREAM,
            'have already been confirmed',
        ),
        (
            CorrectionCase.BRACKET_RESET_DELETION,
            'it deletes it',
        ),
    ],
)
def test_each_case_renders_its_own_warning(env, case, expected):
    out = _render_match_actions(
        env, case=case, downstream=[SimpleNamespace(id='m1')]
    )
    assert expected in out


@pytest.mark.parametrize(
    ('case', 'expected_label'),
    [
        (
            CorrectionCase.CONFIRMED_DOWNSTREAM,
            'I acknowledge that the confirmed downstream matches',
        ),
        (
            CorrectionCase.BRACKET_RESET_DELETION,
            'I acknowledge that the bracket-reset match',
        ),
    ],
)
def test_critical_cases_name_what_is_acknowledged(env, case, expected_label):
    out = _render_match_actions(
        env, case=case, downstream=[SimpleNamespace(id='m1')]
    )
    assert 'name="ack_critical"' in out
    assert expected_label in out


@pytest.mark.parametrize(
    'case',
    [CorrectionCase.NO_DOWNSTREAM, CorrectionCase.UNCONFIRMED_DOWNSTREAM],
)
def test_non_critical_cases_render_no_acknowledgement(env, case):
    out = _render_match_actions(env, case=case)
    assert 'ack_critical' not in out
    assert 'ack_match_ids' not in out


@pytest.mark.parametrize(
    'case',
    [
        CorrectionCase.CONFIRMED_DOWNSTREAM,
        CorrectionCase.BRACKET_RESET_DELETION,
    ],
)
def test_critical_cases_post_the_matches_shown(env, case):
    out = _render_match_actions(
        env,
        case=case,
        downstream=[SimpleNamespace(id='m1'), SimpleNamespace(id='m2')],
        ack_match_ids=['m1', 'm2'],
    )
    assert 'name="ack_match_ids" value="m1,m2"' in out


def test_bracket_match_offers_correction_not_unconfirm(env):
    out = _render_match_actions(env, case=CorrectionCase.NO_DOWNSTREAM)
    assert 'orga_correction_reason' in out
    assert 'orga_unconfirm_reason' not in out


def test_ffa_match_offers_unconfirm_not_correction(env):
    out = _render_match_actions(env, case=None, is_ffa=True)
    assert 'orga_unconfirm_reason' in out
    assert 'orga_correction_reason' not in out


def test_unconfirmed_ffa_match_offers_one_submit_for_the_whole_result(env):
    """Placements and confirmation are a single form and a single button.

    The panel used to post the placements first and only then render a
    separate confirm form, which left a match sitting half-entered
    between the two clicks.
    """
    out = _render_match_actions(env, case=None, is_ffa=True, confirmed=None)
    assert '.orga_submit_ffa_result' in out
    assert out.count('<form') == 2  # the result form plus the comment form
    assert 'name="placement_u1"' in out
    assert 'name="placement_u2"' in out
    assert 'name="score_u1"' not in out


def test_ffa_placement_selects_preselect_the_stored_placements(env):
    placed = [_solo('u1', placement=2), _solo('u2', placement=1)]
    out = _render_match_actions(
        env, case=None, is_ffa=True, confirmed=None, contestants=placed
    )
    assert '<option value="2" selected>' in out


def test_ffa_submit_offered_while_a_placement_is_still_missing(env):
    """One button, always offered.

    Completeness is enforced by `required` on the selects and, for
    anything that gets past the browser, by the service.
    """
    partial = [_solo('u1', placement=1), _solo('u2')]
    out = _render_match_actions(
        env, case=None, is_ffa=True, confirmed=None, contestants=partial
    )
    assert '.orga_submit_ffa_result' in out


def test_confirmed_ffa_match_offers_no_placement_form(env):
    out = _render_match_actions(env, case=None, is_ffa=True)
    assert '.orga_submit_ffa_result' not in out


def test_ffa_placement_form_hidden_unless_editable(env):
    out = _render_match_actions(
        env,
        case=None,
        is_ffa=True,
        confirmed=None,
        results_editable=False,
    )
    assert '.orga_submit_ffa_result' not in out


def test_consumed_ffa_result_shows_notice_instead_of_unconfirm(env):
    out = _render_match_actions(
        env, case=None, is_ffa=True, ffa_result_consumed=True
    )
    assert 'A later round has already been built from this result' in out
    assert 'orga_unconfirm_reason' not in out


def test_walkover_shows_notice_instead_of_correction(env):
    out = _render_match_actions(env, case=None, is_walkover=True)
    assert 'decided by a walkover' in out
    assert 'orga_correction_reason' not in out
    assert 'orga_unconfirm_reason' not in out


@pytest.mark.parametrize('confirmed', ['orga', None])
def test_result_forms_hidden_unless_editable(env, confirmed):
    out = _render_match_actions(
        env,
        case=CorrectionCase.NO_DOWNSTREAM if confirmed else None,
        confirmed=confirmed,
        results_editable=False,
    )
    assert 'Tournament is not in progress.' in out
    assert 'name="score_u1"' not in out
    assert 'orga_correction_reason' not in out
    assert 'orga_unconfirm_reason' not in out
    assert 'orga_comment' in out


def test_downstream_matches_are_listed(env):
    out = _render_match_actions(
        env,
        case=CorrectionCase.CONFIRMED_DOWNSTREAM,
        downstream=[SimpleNamespace(id='m1'), SimpleNamespace(id='m2')],
    )
    assert 'm1' in out
    assert 'm2' in out


def test_unconfirmed_match_renders_confirm_form_and_no_panel(env):
    out = _render_match_actions(env, case=None, confirmed=None)
    assert 'name="score_u1"' in out
    assert 'ack_critical' not in out


def test_score_inputs_are_keyed_by_contestant_not_position(env):
    out = _render_match_actions(env, case=CorrectionCase.NO_DOWNSTREAM)
    assert 'name="corrected_score_u1"' in out
    assert 'name="corrected_score_u2"' in out


def test_status_actions_render_per_status(env):
    tmpl = env.get_template('orga')
    for status, expected in (
        ('REGISTRATION_CLOSED', 'Start tournament'),
        ('ONGOING', 'Pause'),
        ('PAUSED', 'Resume'),
    ):
        out = tmpl.module.render_orga_tournament_status_actions(
            SimpleNamespace(
                id='t0', tournament_status=SimpleNamespace(name=status)
            ),
            True,
        )
        assert expected in out
