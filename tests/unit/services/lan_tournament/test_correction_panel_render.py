"""
tests.unit.services.lan_tournament.test_correction_panel_render
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Render the admin correction panel under ``StrictUndefined``.

The template selects its warning by comparing ``correction_case.value``
against a literal. A typo there is silent -- Jinja renders nothing and
the CONFIRMED_DOWNSTREAM panel, the one warning an admin that
confirmed results are about to be retracted, simply disappears. These
tests pin each case to its own visible text, and pin the score inputs
to contestant keys rather than positions.
"""

import pathlib
import re
from types import SimpleNamespace

import pytest
from jinja2 import DictLoader, Environment, StrictUndefined

from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    DownstreamImpact,
)
from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
)


_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament/view_match.html'
)


@pytest.fixture(scope='module')
def env():
    src = _TEMPLATE.read_text()
    start = src.index("<div class='box block' id='correction'>")
    end = src.index('</form>', src.index('Correct Result')) + len('</form>')

    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader({'panel': src[start:end]}),
    )
    e.globals['_'] = lambda s, **kw: s
    e.globals['render_icon'] = lambda *a, **k: ''
    e.globals['render_match_ref'] = lambda m: str(getattr(m, 'id', m))
    e.globals['render_tag'] = (
        lambda label, **k: f'<span class="tag">{label}</span>'
    )
    e.globals['url_for'] = lambda *a, **k: '/x'
    return e


def _impact(
    match_id='m1',
    *,
    label='Winners bracket, round 2, match 1',
    status='confirmed',
    status_label='Confirmed',
    impact_label='Result will be retracted',
    destructive=True,
    contestants=(),
    open_slots=0,
):
    """One cascade row, as the view builds it."""
    return DownstreamImpact(
        match=SimpleNamespace(id=match_id),
        label=label,
        status=status,
        status_label=status_label,
        impact_label=impact_label,
        destructive=destructive,
        contestants=list(contestants),
        open_slots=open_slots,
    )


def _render(
    env,
    *,
    case,
    contestants,
    downstream=(),
    clears_winner=False,
    ack_match_ids=(),
):
    return env.get_template('panel').render(
        match=SimpleNamespace(id='m0', confirmed_by='admin'),
        ack_match_ids=list(ack_match_ids),
        contestants=list(contestants),
        correction_case=case,
        correction_clears_winner=clears_winner,
        downstream_impact=list(downstream),
        teams_by_id={'t1': SimpleNamespace(id='t1', name='Team Alpha')},
        participants_by_id={
            'u1': SimpleNamespace(id='u1', screen_name='PlayerOne')
        },
        max_match_score=999_999_999,
    )


def _solo(key, score=None):
    return SimpleNamespace(team_id=None, participant_id=key, score=score)


def _pair():
    return [_solo('u1', 3), _solo('u2', 1)]


# ------------------------------------------------------------------ #
# each case renders its own warning
# ------------------------------------------------------------------ #


def test_no_downstream_renders_info_notice(env):
    html = _render(env, case=CorrectionCase.NO_DOWNSTREAM, contestants=_pair())

    assert 'No downstream matches are affected' in html
    assert 'not yet confirmed' not in html
    assert 'Critical' not in html
    # No acknowledgement checkbox where nothing destructive happens.
    assert 'ack_critical' not in html


def test_unconfirmed_downstream_renders_warning_and_list(env):
    html = _render(
        env,
        case=CorrectionCase.UNCONFIRMED_DOWNSTREAM,
        contestants=_pair(),
        downstream=[_impact('m1'), _impact('m2')],
    )

    assert 'not yet confirmed' in html
    assert 'Critical' not in html
    assert 'm1' in html and 'm2' in html
    assert 'ack_critical' not in html


def test_confirmed_downstream_renders_critical_warning_and_ack(env):
    """The destructive case must warn AND demand acknowledgement."""
    html = _render(
        env,
        case=CorrectionCase.CONFIRMED_DOWNSTREAM,
        contestants=_pair(),
        downstream=[_impact('m1')],
    )

    assert 'Critical' in html
    assert 'retract' in html
    assert 'ack_critical' in html
    assert 'm1' in html


def test_every_case_member_renders_a_notice(env):
    """No enum member may fall through the template silently.

    Guards the literal-comparison typo: renaming a member without
    updating the template would drop that case's panel.
    """
    for case in CorrectionCase:
        html = _render(env, case=case, contestants=_pair(),
                       downstream=[_impact('m1')])
        assert 'notification' in html, f'{case.name} rendered no notice'


def test_unclassified_case_renders_no_notice_but_still_renders(env):
    """A None case (classification failed) must not break the page."""
    html = _render(env, case=None, contestants=_pair())

    assert 'Correct Result' in html
    assert 'notification' not in html


# ------------------------------------------------------------------ #
# score inputs are keyed, never positional
# ------------------------------------------------------------------ #


def _input_names(html):
    return re.findall(r"name='(corrected_score_[^']*)'", html)


def test_inputs_are_named_by_contestant_key(env):
    html = _render(env, case=CorrectionCase.NO_DOWNSTREAM, contestants=_pair())

    assert _input_names(html) == [
        'corrected_score_u1',
        'corrected_score_u2',
    ]
    assert 'corrected_score_home' not in html
    assert 'corrected_score_away' not in html


def test_input_names_follow_the_contestant_not_the_slot(env):
    """Reversing row order reverses the inputs, keys intact."""
    forward = _input_names(
        _render(env, case=CorrectionCase.NO_DOWNSTREAM, contestants=_pair())
    )
    backward = _input_names(
        _render(
            env,
            case=CorrectionCase.NO_DOWNSTREAM,
            contestants=list(reversed(_pair())),
        )
    )

    assert sorted(forward) == sorted(backward)
    assert backward == list(reversed(forward))


def test_defwin_slot_emits_no_input(env):
    """A DEFWIN row has no key and must not become corrected_score_None."""
    html = _render(
        env,
        case=CorrectionCase.NO_DOWNSTREAM,
        contestants=[
            _solo('u1', 1),
            SimpleNamespace(team_id=None, participant_id=None, score=None),
        ],
    )

    assert _input_names(html) == ['corrected_score_u1']
    assert 'corrected_score_None' not in html


def test_team_contestants_are_keyed_by_team_id(env):
    html = _render(
        env,
        case=CorrectionCase.NO_DOWNSTREAM,
        contestants=[
            SimpleNamespace(team_id='t1', participant_id=None, score=2),
            SimpleNamespace(team_id='t2', participant_id=None, score=0),
        ],
    )

    assert _input_names(html) == [
        'corrected_score_t1',
        'corrected_score_t2',
    ]
    # Known team resolves to its name; unknown falls back to the key.
    assert 'Team Alpha' in html
    assert 't2' in html


def test_unscored_contestant_renders_empty_value(env):
    html = _render(
        env,
        case=CorrectionCase.NO_DOWNSTREAM,
        contestants=[_solo('u1', None), _solo('u2', None)],
    )

    assert "value=''" in html


# ------------------------------------------------------------------ #
# the cascade ledger
# ------------------------------------------------------------------ #


def test_cascade_row_shows_position_status_and_scores(env):
    """A row must be readable without opening the match.

    The panel used to print the bracket shorthand and nothing else.
    """
    html = _render(
        env,
        case=CorrectionCase.CONFIRMED_DOWNSTREAM,
        contestants=_pair(),
        downstream=[
            _impact(
                'm1',
                label='Winners bracket, round 2, match 1',
                status_label='Confirmed',
                impact_label='Result will be retracted',
                contestants=[_solo('u1', 4), _solo('u2', 2)],
            )
        ],
    )

    assert 'm1' in html  # the shorthand is kept
    assert 'Winners bracket, round 2, match 1' in html
    assert 'Confirmed' in html
    assert 'Result will be retracted' in html
    assert 'PlayerOne' in html
    assert '>4<' in html and '>2<' in html
    assert 'Open match' in html


def test_cascade_marks_only_destructive_rows(env):
    """A match that merely loses a contestant is not dressed as a loss."""
    html = _render(
        env,
        case=CorrectionCase.CONFIRMED_DOWNSTREAM,
        contestants=_pair(),
        downstream=[
            _impact('m1', status='confirmed', destructive=True),
            _impact(
                'm2',
                status='pending',
                status_label='Pending',
                impact_label='Contestant will be removed',
                destructive=False,
            ),
        ],
    )

    assert html.count('lt-cascade__match--destructive') == 1
    assert 'Contestant will be removed' in html


def test_cascade_shows_open_slots_of_unplayed_matches(env):
    """An empty slot is stated, not left as a blank line."""
    html = _render(
        env,
        case=CorrectionCase.UNCONFIRMED_DOWNSTREAM,
        contestants=_pair(),
        downstream=[
            _impact(
                'm1',
                status='pending',
                status_label='Pending',
                impact_label='Contestant will be removed',
                destructive=False,
                contestants=[_solo('u1', None)],
                open_slots=1,
            )
        ],
    )

    assert 'Not yet determined' in html
    assert html.count("class='lt-cascade__score'") == 2


def test_acknowledgement_is_required_on_the_client_too(env):
    """The checkbox blocks the submit before the round trip does."""
    html = _render(
        env,
        case=CorrectionCase.CONFIRMED_DOWNSTREAM,
        contestants=_pair(),
        downstream=[_impact('m1')],
    )

    at = html.index('ack_critical')
    assert 'required' in html[at - 200 : at + 200]


def test_no_acknowledgement_no_required_checkbox(env):
    """Nothing destructive, nothing to acknowledge."""
    html = _render(env, case=CorrectionCase.NO_DOWNSTREAM, contestants=_pair())

    assert 'ack_critical' not in html
    assert 'lt-correct__consent' not in html


_CLEARS_WINNER = 'clears the tournament winner'


def test_terminal_match_warns_that_the_tournament_winner_is_cleared(env):
    """NO_DOWNSTREAM is true but not the whole story: retracting a
    terminal elimination match also reverts the tournament."""
    html = _render(
        env,
        case=CorrectionCase.NO_DOWNSTREAM,
        contestants=_pair(),
        clears_winner=True,
    )

    assert _CLEARS_WINNER in html


def test_non_terminal_match_does_not_warn_about_the_winner(env):
    html = _render(env, case=CorrectionCase.NO_DOWNSTREAM, contestants=_pair())

    assert _CLEARS_WINNER not in html


def test_critical_cases_do_not_repeat_the_winner_warning(env):
    """Their own text already says the winner is cleared."""
    for case in (
        CorrectionCase.CONFIRMED_DOWNSTREAM,
        CorrectionCase.BRACKET_RESET_DELETION,
    ):
        html = _render(
            env,
            case=case,
            contestants=_pair(),
            downstream=[_impact('m1')],
            clears_winner=True,
        )

        assert html.count(_CLEARS_WINNER) == 0


# ------------------------------------------------------------------ #
# a walkover gets a notice, not the correction form
# ------------------------------------------------------------------ #


@pytest.fixture(scope='module')
def panel_region_env():
    """The walkover notice, the panel, and the condition between them."""
    src = _TEMPLATE.read_text()
    start = src.index('{%- if match.confirmed_by and is_walkover')
    form_end = src.index('</form>', src.index('Correct Result'))
    end = src.index('{%- endif %}', form_end) + len('{%- endif %}')

    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader({'region': src[start:end]}),
    )
    e.globals['_'] = lambda s, **kw: s
    e.globals['render_icon'] = lambda *a, **k: ''
    e.globals['render_match_ref'] = lambda m: str(getattr(m, 'id', m))
    e.globals['render_tag'] = (
        lambda label, **k: f'<span class="tag">{label}</span>'
    )
    e.globals['url_for'] = lambda *a, **k: '/x'
    e.globals['g'] = SimpleNamespace(
        user=SimpleNamespace(has_permission=lambda _p: True)
    )
    return e


def _render_region(env, *, is_walkover, contestants):
    return env.get_template('region').render(
        match=SimpleNamespace(id='m0', confirmed_by='admin'),
        is_walkover=is_walkover,
        contestants=list(contestants),
        correction_case=None,
        ack_match_ids=[],
        correction_clears_winner=False,
        downstream_impact=[],
        teams_by_id={},
        participants_by_id={},
        max_match_score=999_999_999,
    )


def test_walkover_renders_notice_instead_of_the_form(panel_region_env):
    html = _render_region(
        panel_region_env, is_walkover=True, contestants=[_solo('u1')]
    )

    assert 'walkover' in html
    assert "name='reason'" not in html
    assert 'corrected_score_' not in html


def test_played_match_still_renders_the_form(panel_region_env):
    html = _render_region(
        panel_region_env, is_walkover=False, contestants=_pair()
    )

    assert "name='reason'" in html
    assert 'walkover' not in html


def test_acknowledgement_posts_the_matches_it_covers(env):
    html = _render(
        env,
        case=CorrectionCase.CONFIRMED_DOWNSTREAM,
        contestants=_pair(),
        downstream=[_impact('m1')],
        ack_match_ids=['m1', 'm2'],
    )

    assert "name='ack_match_ids' value='m1,m2'" in html


@pytest.fixture(scope='module')
def ffa_unconfirm_env():
    """The FFA unconfirm block and the condition around it."""
    src = _TEMPLATE.read_text()
    form = src.index("url_for('.unconfirm_match'")
    start = src.rindex('{%- if match.confirmed_by and', 0, form)
    close = src.index('</div>', src.index('</form>', form))
    end = src.index('{%- endif %}', close) + len('{%- endif %}')

    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader({'ffa_unconfirm': src[start:end]}),
    )
    e.globals['_'] = lambda s, **kw: s
    e.globals['render_icon'] = lambda *a, **k: ''
    e.globals['url_for'] = lambda *a, **k: '/x'
    e.globals['g'] = SimpleNamespace(
        user=SimpleNamespace(has_permission=lambda _p: True)
    )
    return e


def _render_ffa_unconfirm(env, *, ffa_result_consumed):
    return env.get_template('ffa_unconfirm').render(
        match=SimpleNamespace(id='m0', confirmed_by='admin'),
        ffa_result_consumed=ffa_result_consumed,
    )


def test_advanced_ffa_group_renders_notice_instead_of_the_form(
    ffa_unconfirm_env,
):
    html = _render_ffa_unconfirm(ffa_unconfirm_env, ffa_result_consumed=True)

    assert 'A later round has already been built' in html
    assert "name='reason'" not in html


def test_latest_ffa_group_still_renders_the_form(ffa_unconfirm_env):
    html = _render_ffa_unconfirm(ffa_unconfirm_env, ffa_result_consumed=False)

    assert "name='reason'" in html
    assert 'A later round has already been built' not in html
