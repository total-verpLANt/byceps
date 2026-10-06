from dataclasses import replace
from datetime import datetime, timedelta, UTC
from uuid import uuid4

from flask import Flask
from flask_babel import Babel
import pytest

from byceps.services.lan_tournament import tournament_domain_service
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_status import (
    ContestantStatus,
)
from byceps.services.lan_tournament.models.contestant_type import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_personal_service import (
    PersonalGroup,
    evaluate_participation,
)


@pytest.fixture(autouse=True)
def locale():
    app = Flask(__name__)
    Babel(app, default_locale='en')
    with app.app_context():
        yield


def tournament(**kwargs):
    return tournament_domain_service.create_tournament(
        'party',
        'Cup',
        **{
            'tournament_status': TournamentStatus.ONGOING,
            'game_format': GameFormat.ONE_V_ONE,
            'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
            'score_ordering': ScoreOrdering.HIGHER_IS_BETTER,
            **kwargs,
        },
    )[0]


def participant(t, **kwargs):
    return TournamentParticipant(
        id=uuid4(),
        user_id=uuid4(),
        tournament_id=t.id,
        substitute_player=False,
        team_id=None,
        created_at=datetime.now(UTC),
        **kwargs,
    )


def match(t, **kwargs):
    return TournamentMatch(
        **{
            'id': uuid4(),
            'tournament_id': t.id,
            'group_order': None,
            'match_order': 0,
            'round': 0,
            'next_match_id': None,
            'confirmed_by': None,
            'created_at': datetime.now(UTC),
            **kwargs,
        }
    )


def side(m, p=None, **kwargs):
    return TournamentMatchToContestant(
        **{
            'id': uuid4(),
            'tournament_match_id': m.id,
            'participant_id': p.id if p else uuid4(),
            'team_id': None,
            'score': None,
            'created_at': datetime.now(UTC),
            **kwargs,
        }
    )


def evaluate(t, p, *entries):
    return evaluate_participation(
        t, p, [m for m, _ in entries], dict((m.id, cs) for m, cs in entries)
    )


@pytest.mark.parametrize(
    'state,group,status',
    [
        (
            TournamentStatus.REGISTRATION_OPEN,
            PersonalGroup.REGISTERED,
            'Registered',
        ),
        (
            TournamentStatus.REGISTRATION_CLOSED,
            PersonalGroup.REGISTERED,
            'Registered',
        ),
        (
            TournamentStatus.ONGOING,
            PersonalGroup.WAITING,
            'Waiting for matches',
        ),
        (TournamentStatus.PAUSED, PersonalGroup.WAITING, 'Paused'),
        (TournamentStatus.CANCELLED, PersonalGroup.FINISHED, 'Cancelled'),
        (TournamentStatus.COMPLETED, PersonalGroup.FINISHED, 'Completed'),
    ],
)
def test_tournament_lifecycle(state, group, status):
    t = tournament(tournament_status=state)
    result = evaluate(t, participant(t))
    assert result.group == group
    assert str(result.status) == status


def test_waiting_opponent_and_confirmed_bye_are_not_elimination():
    t = tournament()
    p = participant(t)
    future = match(t, round=1)
    bye = match(t, confirmed_by=uuid4(), next_match_id=future.id)
    result = evaluate(t, p, (bye, [side(bye, p)]), (future, [side(future, p)]))
    assert result.group == PersonalGroup.WAITING
    assert str(result.status) == 'Waiting for an opponent'
    assert [e.match.id for e in result.matches] == [future.id]


@pytest.mark.parametrize(
    'mode,bracket',
    [
        (EliminationMode.SINGLE_ELIMINATION, Bracket.THIRD_PLACE),
        (EliminationMode.DOUBLE_ELIMINATION, Bracket.LOSERS),
    ],
)
def test_loss_with_saved_loser_routing_remains_active(mode, bracket):
    t = tournament(elimination_mode=mode)
    p = participant(t)
    future = match(t, bracket=bracket)
    loss = match(t, confirmed_by=uuid4(), loser_next_match_id=future.id)
    result = evaluate(
        t,
        p,
        (loss, [side(loss, p, score=0), side(loss, score=1)]),
        (future, [side(future, p)]),
    )
    assert result.group == PersonalGroup.WAITING
    assert result.matches[0].match.bracket == bracket
    future = replace(future, confirmed_by=uuid4())
    result = evaluate(
        t,
        p,
        (loss, [side(loss, p, score=0), side(loss, score=1)]),
        (future, [side(future, p, score=0), side(future, score=1)]),
    )
    assert result.group == PersonalGroup.FINISHED
    assert str(result.status) == 'Eliminated'


def test_missing_advancement_is_reviewable_not_guessed():
    t = tournament()
    p = participant(t)
    m = match(t, confirmed_by=uuid4(), next_match_id=uuid4())
    result = evaluate(t, p, (m, [side(m, p, score=2), side(m, score=0)]))
    assert result.group == PersonalGroup.WAITING
    assert str(result.status) == 'Participation needs review'
    assert result.needs_attention


def test_saved_grand_final_reset_keeps_both_contestants_active():
    t = tournament(elimination_mode=EliminationMode.DOUBLE_ELIMINATION)
    p = participant(t)
    reset = match(t, bracket=Bracket.GRAND_FINAL, match_order=1)
    final = match(
        t,
        bracket=Bracket.GRAND_FINAL,
        confirmed_by=uuid4(),
        next_match_id=reset.id,
    )
    cs = [side(final, p, score=0), side(final, score=1)]
    result = evaluate(t, p, (final, cs), (reset, [side(reset, p), side(reset)]))
    assert result.group == PersonalGroup.ONGOING
    assert [e.match.id for e in result.matches] == [reset.id]
    reset = replace(reset, confirmed_by=uuid4())
    result = evaluate(
        t,
        p,
        (final, cs),
        (reset, [side(reset, p, score=0), side(reset, score=1)]),
    )
    assert result.group == PersonalGroup.FINISHED
    assert str(result.status) == 'Eliminated'


def test_completed_with_pending_p3_is_explicit_engine_inconsistency():
    t = tournament(tournament_status=TournamentStatus.COMPLETED)
    p = participant(t)
    m = match(t, bracket=Bracket.THIRD_PLACE)
    result = evaluate(t, p, (m, [side(m, p), side(m)]))
    assert result.group == PersonalGroup.WAITING
    assert (
        str(result.status)
        == 'Tournament completed with an open third-place match'
    )
    assert result.matches[0].match.id == m.id
    assert result.needs_attention


def test_round_robin_keeps_multiple_open_matches_sorted_without_loss_elimination():
    t = tournament(elimination_mode=EliminationMode.ROUND_ROBIN)
    p = participant(t)
    lost = match(t, confirmed_by=uuid4())
    later = match(t, round=2)
    earlier = match(t, round=1)
    result = evaluate(
        t,
        p,
        (later, [side(later, p), side(later)]),
        (lost, [side(lost, p, score=0), side(lost, score=1)]),
        (earlier, [side(earlier, p), side(earlier)]),
    )
    assert result.group == PersonalGroup.ONGOING
    assert [e.match.id for e in result.matches] == [earlier.id, later.id]
    assert (
        evaluate(
            t, p, (lost, [side(lost, p, score=0), side(lost, score=1)])
        ).group
        == PersonalGroup.FINISHED
    )


def test_result_retraction_and_reopening_use_fresh_state():
    t = tournament()
    p = participant(t)
    m = match(t, confirmed_by=uuid4())
    cs = [side(m, p, score=0), side(m, score=1)]
    assert evaluate(t, p, (m, cs)).group == PersonalGroup.FINISHED
    assert (
        evaluate(t, p, (replace(m, confirmed_by=None), cs)).group
        == PersonalGroup.ONGOING
    )
    highscore = replace(
        t,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        tournament_status=TournamentStatus.COMPLETED,
    )
    assert evaluate(highscore, p).group == PersonalGroup.FINISHED
    assert (
        evaluate(
            replace(highscore, tournament_status=TournamentStatus.ONGOING), p
        ).group
        == PersonalGroup.ONGOING
    )


def test_current_team_not_former_team_or_solo_side():
    t = tournament(contestant_type=ContestantType.TEAM)
    p = replace(participant(t), team_id=uuid4())
    current = match(t)
    former = match(t)
    result = evaluate(
        t,
        p,
        (current, [side(current, team_id=p.team_id, participant_id=None)]),
        (
            former,
            [
                side(former, p),
                side(former, team_id=uuid4(), participant_id=None),
            ],
        ),
    )
    assert [e.match.id for e in result.matches] == [current.id]
    result = evaluate(
        t,
        replace(p, team_id=None),
        (current, [side(current, team_id=p.team_id, participant_id=None)]),
    )
    assert result.matches == []
    assert str(result.status) == 'No current team'
    assert result.group == PersonalGroup.WAITING
    assert result.needs_attention


def test_highscore_never_invents_matches_even_with_stray_match_data():
    t = tournament(
        game_format=GameFormat.HIGHSCORE, elimination_mode=EliminationMode.NONE
    )
    p = participant(t)
    m = match(t)
    result = evaluate(t, p, (m, [side(m, p)]))
    assert result.matches == []
    assert str(result.status) == 'Submit a score'


def test_missing_format_data_cannot_establish_elimination():
    t = tournament(game_format=None, elimination_mode=None)
    p = participant(t)
    m = match(t, confirmed_by=uuid4())
    result = evaluate(t, p, (m, [side(m, p, score=0), side(m, score=1)]))
    assert result.group == PersonalGroup.WAITING
    assert str(result.status) == 'Participation needs review'


@pytest.mark.parametrize(
    'bracket,expected',
    [
        (None, PersonalGroup.FINISHED),
        (Bracket.WINNERS, PersonalGroup.WAITING),
        (Bracket.LOSERS, PersonalGroup.FINISHED),
    ],
)
def test_ffa_confirmed_cutoff_uses_engine_points_and_pool(bracket, expected):
    t = tournament(game_format=GameFormat.FREE_FOR_ALL, advancement_count=1)
    p = participant(t)
    m = match(t, bracket=bracket, confirmed_by=uuid4())
    result = evaluate(
        t,
        p,
        (
            m,
            [side(m, p, placement=2, points=1), side(m, placement=1, points=3)],
        ),
    )
    assert result.group == expected


def test_ffa_tie_and_unconfirmed_other_group_block_elimination():
    t = tournament(game_format=GameFormat.FREE_FOR_ALL, advancement_count=1)
    p = participant(t)
    m = match(t, confirmed_by=uuid4())
    tied = [side(m, p, placement=2, points=1), side(m, placement=1, points=1)]
    assert str(evaluate(t, p, (m, tied)).status) == 'Advancement cutoff tied'
    other = match(t, match_order=1)
    cs = [side(m, p, placement=2, points=0), side(m, placement=1, points=1)]
    assert (
        evaluate(t, p, (m, cs), (other, [side(other)])).group
        == PersonalGroup.WAITING
    )


def test_ffa_current_lb_round_overrides_old_wb_and_match_dq_is_not_lifecycle():
    t = tournament(game_format=GameFormat.FREE_FOR_ALL, advancement_count=1)
    p = participant(t)
    wb = match(t, bracket=Bracket.WINNERS, round=4, confirmed_by=uuid4())
    lb = match(
        t,
        bracket=Bracket.LOSERS,
        round=0,
        created_at=wb.created_at + timedelta(seconds=1),
    )
    result = evaluate(
        t,
        p,
        (wb, [side(wb, p, placement=2, points=0)]),
        (lb, [side(lb, p, contestant_status=ContestantStatus.DQ), side(lb)]),
    )
    assert result.group == PersonalGroup.ONGOING
    assert result.matches[0].own_side.contestant_status == ContestantStatus.DQ


@pytest.mark.parametrize(
    'state', [TournamentStatus.ONGOING, TournamentStatus.COMPLETED]
)
def test_ffa_grand_final_missing_first_loss_wb_side_is_not_hidden(state):
    t = tournament(
        game_format=GameFormat.FREE_FOR_ALL,
        advancement_count=1,
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        tournament_status=state,
    )
    p = participant(t)
    wb = match(t, bracket=Bracket.WINNERS, confirmed_by=uuid4())
    gf = match(t, bracket=Bracket.GRAND_FINAL)
    result = evaluate(
        t,
        p,
        (wb, [side(wb, p, placement=2, points=0)]),
        (gf, [side(gf), side(gf)]),
    )
    assert str(result.status) == 'Participation needs review'
    assert result.group == PersonalGroup.WAITING
    assert result.needs_attention


@pytest.mark.parametrize('scores', [(None, None), (0, 2), (2, 0)])
def test_entered_scores_have_no_completion_effect(scores):
    t = tournament()
    p = participant(t)
    m = match(t)
    cs = [side(m, p, score=scores[0]), side(m, score=scores[1])]
    result = evaluate(t, p, (m, cs))
    assert result.group == PersonalGroup.ONGOING
    assert result.matches[0].state == 'open'
    assert str(result.status) == 'Open matches'
    if scores[0] is not None:
        confirmed = evaluate(t, p, (replace(m, confirmed_by=p.user_id), cs))
        assert confirmed.group == PersonalGroup.FINISHED


def test_unconfirmed_ffa_placements_and_points_do_not_advance():
    t = tournament(game_format=GameFormat.FREE_FOR_ALL, advancement_count=1)
    p = participant(t)
    m = match(t)
    cs = [side(m, p, placement=2, points=0), side(m, placement=1, points=3)]
    assert evaluate(t, p, (m, cs)).group == PersonalGroup.ONGOING
    assert (
        evaluate(t, p, (replace(m, confirmed_by=p.user_id), cs)).group
        == PersonalGroup.FINISHED
    )


@pytest.mark.parametrize('team', [False, True])
def test_empty_opponent_placeholder_does_not_occupy_match(team):
    t = tournament(
        contestant_type=ContestantType.TEAM if team else ContestantType.SOLO
    )
    p = participant(t)
    if team:
        p = replace(p, team_id=uuid4())
    m = match(t)
    own = (
        side(m, p)
        if not team
        else side(m, participant_id=None, team_id=p.team_id)
    )
    result = evaluate(t, p, (m, [own, side(m, participant_id=None)]))
    assert result.group == PersonalGroup.WAITING
    assert result.matches[0].opponents == []
    assert result.ready_matches == []
    assert not result.needs_attention


@pytest.mark.parametrize('opponent_ready', [False, True])
@pytest.mark.parametrize('won', [False, True])
def test_only_confirmation_exposes_saved_next_match(opponent_ready, won):
    t = tournament(elimination_mode=EliminationMode.DOUBLE_ELIMINATION)
    p = participant(t)
    future = match(t, round=1)
    previous = match(
        t,
        next_match_id=future.id if won else None,
        loser_next_match_id=None if won else future.id,
    )
    cs = [
        side(previous, p, score=2 if won else 0),
        side(previous, score=0 if won else 2),
    ]
    before = evaluate(t, p, (previous, cs))
    assert before.group == PersonalGroup.ONGOING
    # The engine materializes the user's next side when it confirms the result.
    future_sides = [side(future, p)] + (
        [side(future)] if opponent_ready else []
    )
    after = evaluate(
        t,
        p,
        (replace(previous, confirmed_by=p.user_id), cs),
        (future, future_sides),
    )
    assert after.group == (
        PersonalGroup.ONGOING if opponent_ready else PersonalGroup.WAITING
    )
    assert [e.match.id for e in after.matches] == [future.id]


def test_mixed_matches_are_partitioned_and_sorted_ready_first():
    t = tournament(elimination_mode=EliminationMode.ROUND_ROBIN)
    p = participant(t)
    pending = match(t, round=0)
    later = match(t, round=2)
    earlier = match(t, round=1)
    result = evaluate(
        t,
        p,
        (pending, [side(pending, p)]),
        (later, [side(later, p), side(later)]),
        (earlier, [side(earlier, p), side(earlier)]),
    )
    assert result.group == PersonalGroup.ONGOING
    assert [m.match.id for m in result.matches] == [
        earlier.id,
        later.id,
        pending.id,
    ]
    assert result.ready_matches == result.matches[:2]
    assert result.waiting_matches == result.matches[2:]


@pytest.mark.parametrize(
    'format,mode',
    [
        (GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION),
        (GameFormat.HIGHSCORE, EliminationMode.NONE),
    ],
)
def test_pause_never_suggests_active_play(format, mode):
    t = tournament(
        game_format=format,
        elimination_mode=mode,
        tournament_status=TournamentStatus.PAUSED,
    )
    p = participant(t)
    m = match(t)
    result = evaluate(t, p, (m, [side(m, p), side(m)]))
    assert result.group == PersonalGroup.WAITING
    assert result.state == 'notice'
    assert str(result.status) == 'Paused'


def test_registration_precedes_stray_open_match_data():
    t = tournament(tournament_status=TournamentStatus.REGISTRATION_OPEN)
    p = participant(t)
    m = match(t)
    assert (
        evaluate(t, p, (m, [side(m, p), side(m)])).group
        == PersonalGroup.REGISTERED
    )


@pytest.mark.parametrize(
    'state,group,status,awaiting',
    [
        (
            TournamentStatus.ONGOING,
            PersonalGroup.WAITING,
            'Waiting for the tournament orga to complete the tournament',
            True,
        ),
        (TournamentStatus.PAUSED, PersonalGroup.WAITING, 'Paused', False),
        (
            TournamentStatus.REGISTRATION_OPEN,
            PersonalGroup.REGISTERED,
            'Registered',
            False,
        ),
        (
            TournamentStatus.REGISTRATION_CLOSED,
            PersonalGroup.REGISTERED,
            'Registered',
            False,
        ),
        (
            TournamentStatus.COMPLETED,
            PersonalGroup.FINISHED,
            'Completed',
            False,
        ),
        (
            TournamentStatus.CANCELLED,
            PersonalGroup.FINISHED,
            'Cancelled',
            False,
        ),
    ],
)
def test_complete_highscore_respects_lifecycle(state, group, status, awaiting):
    t = tournament(
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        tournament_status=state,
    )
    result = evaluate_participation(
        t, participant(t), [], {}, highscore_results_complete=True
    )
    assert result.group == group
    assert str(result.status) == status
    assert result.awaiting_completion == awaiting
    assert not result.show_score_submission_prompt
    assert result.needs_attention == (state == TournamentStatus.PAUSED)
    if awaiting:
        assert result.state == 'waiting'


@pytest.mark.parametrize(
    'changes,status',
    [
        ({'score_ordering': None}, 'Participation needs review'),
        ({'elimination_mode': None}, 'Participation needs review'),
        (
            {'elimination_mode': EliminationMode.SINGLE_ELIMINATION},
            'Participation needs review',
        ),
        ({'contestant_type': ContestantType.TEAM}, 'No current team'),
    ],
)
def test_highscore_coverage_cannot_hide_review(changes, status):
    t = tournament(
        **{
            'game_format': GameFormat.HIGHSCORE,
            'elimination_mode': EliminationMode.NONE,
            **changes,
        }
    )
    result = evaluate_participation(
        t, participant(t), [], {}, highscore_results_complete=True
    )
    assert str(result.status) == status
    assert result.needs_attention and not result.awaiting_completion
    assert not result.show_score_submission_prompt


def test_incomplete_highscore_and_match_formats_keep_existing_actions():
    t = tournament(
        game_format=GameFormat.HIGHSCORE, elimination_mode=EliminationMode.NONE
    )
    result = evaluate(t, participant(t))
    assert result.group == PersonalGroup.ONGOING
    assert result.show_score_submission_prompt
    assert not result.awaiting_completion
    t = tournament()
    result = evaluate_participation(
        t, participant(t), [], {}, highscore_results_complete=True
    )
    assert str(result.status) == 'Waiting for matches'
    assert not result.awaiting_completion
