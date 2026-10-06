from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import patch

from jinja2 import ChoiceLoader, FileSystemLoader
import pytest
from sqlalchemy import delete, event, update

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service as engine,
    tournament_orga_service,
    tournament_personal_service as personal,
    tournament_repository as repository,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
from byceps.services.lan_tournament.dbmodels.score_submission import (
    DbScoreSubmission,
)
from byceps.services.lan_tournament import tournament_personal_repository
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_type import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_category import (
    TournamentCategory,
)
from byceps.services.lan_tournament.models.tournament_team import TournamentTeam
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_personal_service import (
    PersonalGroup,
)
from byceps.services.site.models import SiteID
from byceps.util.uuid import generate_uuid7

from tests.helpers import create_site, http_client, log_in_user
from tests.unit.services.lan_tournament.test_tournament_personal_service import (
    participant,
)


pytestmark = pytest.mark.usefixtures('admin_app', 'email_config')


@pytest.fixture
def setup(make_party, brand, make_user):
    return make_party(brand), make_user(), make_user()


def create(party, name='Personal Cup', **kwargs):
    return tournament_service.create_tournament(
        party.id,
        name,
        **{
            'tournament_status': TournamentStatus.ONGOING,
            'game_format': GameFormat.ONE_V_ONE,
            'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
            **kwargs,
        },
    ).unwrap()[0]


def add(tournament, user):
    p = replace(participant(tournament), user_id=user.id)
    repository.create_participant(p)
    repository.commit_session()
    return p


def entry(party, user):
    return personal.get_personal_overview(party.id, user.id)['entries'][0]


def confirm(match, admin, winner=None):
    cs = repository.get_contestants_for_match(match.id)
    if winner is None:
        winner = cs[0].participant_id
    engine.admin_set_and_confirm_match(
        match.id,
        admin.id,
        {c.participant_id: 2 if c.participant_id == winner else 0 for c in cs},
    ).unwrap()


@contextmanager
def queries():
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(db.engine, 'before_cursor_execute', capture)
    try:
        yield statements
    finally:
        event.remove(db.engine, 'before_cursor_execute', capture)


def test_membership_party_scope_removed_rows_and_team_changes(
    setup, make_party, brand
):
    party, user, other = setup
    solo = create(party, 'Own solo')
    add(solo, user)
    foreign_user = create(party, 'Other user')
    add(foreign_user, other)
    foreign_party = create(make_party(brand), 'Other party')
    add(foreign_party, user)
    removed = create(party, 'Removed signup')
    removed_p = add(removed, user)
    db.session.execute(
        update(DbTournamentParticipant)
        .where(DbTournamentParticipant.id == removed_p.id)
        .values(removed_at=datetime.now(UTC))
    )
    draft = create(
        party, 'Hidden draft', tournament_status=TournamentStatus.DRAFT
    )
    add(draft, user)
    team_t = create(party, 'Current team', contestant_type=ContestantType.TEAM)
    p = add(team_t, user)
    team = TournamentTeam(
        id=generate_uuid7(),
        tournament_id=team_t.id,
        name='Active team',
        tag=None,
        description=None,
        image_url=None,
        captain_user_id=user.id,
        join_code=None,
        created_at=datetime.now(UTC),
    )
    repository.create_team(team)
    repository.update_participant(replace(p, team_id=team.id))
    opponent_p = add(team_t, other)
    opponent_team = replace(
        team,
        id=generate_uuid7(),
        name='Opponent team',
        captain_user_id=other.id,
    )
    repository.create_team(opponent_team)
    repository.update_participant(replace(opponent_p, team_id=opponent_team.id))
    engine.generate_single_elimination_bracket(
        team_t.id, initiator_id=other.id
    ).unwrap()
    context = personal.get_personal_overview(party.id, user.id)
    assert {e.tournament.id for e in context['entries']} == {solo.id, team_t.id}
    assert context['teams_by_id'][team.id].name == 'Active team'
    assert entry_for(context, team_t.id).matches[0].own_side.team_id == team.id
    db.session.execute(
        update(DbTournamentTeam)
        .where(DbTournamentTeam.id == team.id)
        .values(removed_at=datetime.now(UTC))
    )
    db.session.commit()
    assert [
        e.tournament.id
        for e in personal.get_personal_overview(party.id, user.id)['entries']
    ] == [solo.id]
    repository.update_participant(replace(p, team_id=None))
    assert (
        entry_for(
            personal.get_personal_overview(party.id, user.id), team_t.id
        ).participant.team_id
        is None
    )
    assert (
        entry_for(
            personal.get_personal_overview(party.id, user.id), team_t.id
        ).matches
        == []
    )


def entry_for(context, tournament_id):
    return next(
        e for e in context['entries'] if e.tournament.id == tournament_id
    )


def test_batch_queries_do_not_grow_per_card_match_or_orga(setup):
    party, user, other = setup

    def populate(name):
        t = create(party, name)
        add(t, user)
        add(t, other)
        engine.generate_single_elimination_bracket(
            t.id, initiator_id=other.id
        ).unwrap()
        tournament_orga_service.assign_orga(t.id, user.id, other.id).unwrap()
        tournament_orga_service.assign_orga(t.id, other.id, other.id).unwrap()

    populate('One')
    with queries() as first:
        personal.get_personal_overview(party.id, user.id)
    for i in range(4):
        populate(f'Extra {i}')
    with queries() as several:
        context = personal.get_personal_overview(party.id, user.id)
    assert len(several) == len(first)
    assert len(several) <= 12
    assert all(sql.lstrip().upper().startswith('SELECT') for sql in several)
    assert len(context['entries']) == 5
    assert all(
        len(orgas) == 2 for orgas in context['orgas_by_tournament'].values()
    )


def test_engine_se_bye_p3_final_before_p3_and_result_retraction(
    setup, make_user
):
    party, user, admin = setup
    t = create(party)
    ps = [
        add(t, u) for u in [user, admin, make_user(), make_user(), make_user()]
    ]
    engine.generate_single_elimination_bracket(
        t.id, initiator_id=admin.id
    ).unwrap()
    snapshot = personal.get_personal_overview(party.id, user.id)
    initial = snapshot['entries'][0]
    assert initial.group == (
        PersonalGroup.ONGOING
        if initial.ready_matches
        else PersonalGroup.WAITING
    )
    # Confirm every non-final main match; P3 remains open.
    for m in repository.get_matches_for_tournament_ordered(t.id):
        if m.bracket == Bracket.THIRD_PLACE or m.confirmed_by:
            continue
        if m.next_match_id is None:
            continue
        cs = repository.get_contestants_for_match(m.id)
        if len(cs) == 2:
            confirm(m, admin)
    matches = repository.get_matches_for_tournament_ordered(t.id)
    final = next(
        m
        for m in matches
        if m.next_match_id is None and m.bracket != Bracket.THIRD_PLACE
    )
    p3 = next(m for m in matches if m.bracket == Bracket.THIRD_PLACE)
    loser_id = repository.get_contestants_for_match(p3.id)[0].participant_id
    loser = next(p for p in ps if p.id == loser_id)
    confirm(final, admin)
    assert (
        repository.get_tournament(t.id).tournament_status
        == TournamentStatus.COMPLETED
    )
    p3_context = personal.get_personal_overview(party.id, loser.user_id)
    assert p3_context['entries'][0].group == PersonalGroup.WAITING
    assert p3_context['entries'][0].needs_attention
    assert p3_context['entries'][0].matches[0].match.id == p3.id
    # This is a pre-existing engine defect: COMPLETED blocks site result entry.
    engine.correct_match_result(
        final.id,
        admin.id,
        reason='Retract final',
        corrected_scores=None,
        ack_critical=True,
        acknowledged_match_ids=[],
    ).unwrap()
    assert (
        repository.get_tournament(t.id).tournament_status
        == TournamentStatus.ONGOING
    )
    reopened = entry(party, user)
    assert reopened.group == (
        PersonalGroup.ONGOING
        if reopened.ready_matches
        else PersonalGroup.WAITING
    )


def test_engine_de_first_loss_lb_and_bracket_reset(setup, make_user):
    party, user, admin = setup
    t = create(party, elimination_mode=EliminationMode.DOUBLE_ELIMINATION)
    for u in [user, admin, make_user(), make_user()]:
        add(t, u)
    engine.generate_double_elimination_bracket(
        t.id, initiator_id=admin.id
    ).unwrap()
    own = entry(party, user)
    m = own.matches[0].match
    opponent = own.matches[0].opponents[0].participant_id
    confirm(m, admin, opponent)
    assert entry(party, user).group == PersonalGroup.WAITING
    assert entry(party, user).matches[0].match.bracket == Bracket.LOSERS
    # Finish each pool, favouring the current user so they become LB champion.
    for bracket in [Bracket.WINNERS, Bracket.LOSERS]:
        for m in repository.get_matches_for_tournament_ordered(t.id):
            if m.bracket != bracket or m.confirmed_by:
                continue
            cs = repository.get_contestants_for_match(m.id)
            own_p = next(
                (
                    c.participant_id
                    for c in cs
                    if c.participant_id == own.participant.id
                ),
                None,
            )
            confirm(m, admin, own_p)
    gf = next(
        m
        for m in repository.get_matches_for_tournament_ordered(t.id)
        if m.bracket == Bracket.GRAND_FINAL
    )
    confirm(gf, admin, own.participant.id)
    reset = entry(party, user).matches[0].match
    assert reset.bracket == Bracket.GRAND_FINAL and reset.match_order == 1
    assert (
        repository.get_tournament(t.id).tournament_status
        == TournamentStatus.ONGOING
    )
    confirm(reset, admin, own.participant.id)
    assert entry(party, user).group == PersonalGroup.FINISHED


def test_engine_round_robin_loss_and_multiple_open_matches(setup, make_user):
    party, user, admin = setup
    t = create(party, elimination_mode=EliminationMode.ROUND_ROBIN)
    for u in [user, admin, make_user(), make_user()]:
        add(t, u)
    engine.generate_round_robin_bracket(t.id, initiator_id=admin.id).unwrap()
    before = entry(party, user)
    assert len(before.matches) == 3
    confirm(
        before.matches[0].match,
        admin,
        before.matches[0].opponents[0].participant_id,
    )
    assert len(entry(party, user).matches) == 2
    assert entry(party, user).group == PersonalGroup.ONGOING


def test_engine_ffa_round_progression_and_cutoff(setup, make_user):
    party, user, admin = setup
    t = create(
        party,
        game_format=GameFormat.FREE_FOR_ALL,
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
        point_table=[4, 3, 2, 1],
    )
    for u in [user, admin, *[make_user() for _ in range(6)]]:
        add(t, u)
    engine.generate_ffa_round(t.id, initiator_id=admin.id).unwrap()
    user_p = next(
        p
        for p in repository.get_participants_for_tournament(t.id)
        if p.user_id == user.id
    )
    for m in repository.get_matches_for_tournament_ordered(t.id):
        cs = repository.get_contestants_for_match(m.id)
        # The user wins their lobby, so the placements do not depend on the
        # seeding order inside it.
        ordered = [c for c in cs if c.participant_id == user_p.id] + [
            c for c in cs if c.participant_id != user_p.id
        ]
        engine.set_ffa_placements(
            m.id,
            {str(c.participant_id): i + 1 for i, c in enumerate(ordered)},
        ).unwrap()
        engine.confirm_ffa_match(m.id, admin.id).unwrap()
    assert entry(party, user).group == PersonalGroup.WAITING
    engine.advance_ffa_round(t.id, initiator_id=admin.id).unwrap()
    assert entry(party, user).matches[0].match.round == 1


def test_engine_ffa_de_gf_collector_keeps_wb_first_loss(setup, make_user):
    party, user, admin = setup
    t = create(
        party,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
        point_table=[4, 3, 2, 1],
    )
    for u in [user, admin, make_user(), make_user()]:
        add(t, u)
    engine.generate_ffa_round(
        t.id, bracket=Bracket.WINNERS, initiator_id=admin.id
    ).unwrap()
    wb = repository.get_matches_for_tournament_ordered(t.id)[0]
    cs = repository.get_contestants_for_match(wb.id)
    # The user is placed below the WB cutoff. The grand final still seeds
    # them, so the overview shows the final instead of asking for review.
    user_p = next(
        p
        for p in repository.get_participants_for_tournament(t.id)
        if p.user_id == user.id
    )
    ordered = [c for c in cs if c.participant_id != user_p.id] + [
        c for c in cs if c.participant_id == user_p.id
    ]
    engine.set_ffa_placements(
        wb.id, {str(c.participant_id): i + 1 for i, c in enumerate(ordered)}
    ).unwrap()
    engine.confirm_ffa_match(wb.id, admin.id).unwrap()
    assert (
        engine.advance_ffa_round(
            t.id, pool=Bracket.WINNERS, initiator_id=admin.id
        ).unwrap()
        == 'grand_final_eligible'
    )
    engine.generate_ffa_grand_final(t.id, initiator_id=admin.id).unwrap()
    gf = next(
        m
        for m in repository.get_matches_for_tournament_ordered(t.id)
        if m.bracket == Bracket.GRAND_FINAL
    )
    assert user_p.id in {
        c.participant_id for c in repository.get_contestants_for_match(gf.id)
    }
    own = entry(party, user)
    assert own.group == PersonalGroup.ONGOING
    assert not own.needs_attention
    assert [m.match.bracket for m in own.matches] == [Bracket.GRAND_FINAL]


def test_actual_result_correction_and_highscore_reopening(setup):
    party, user, admin = setup
    t = create(party)
    p = add(t, user)
    opponent = add(t, admin)
    engine.generate_single_elimination_bracket(
        t.id, initiator_id=admin.id
    ).unwrap()
    m = entry(party, user).matches[0].match
    confirm(m, admin, opponent.id)
    assert entry(party, user).group == PersonalGroup.FINISHED
    engine.correct_match_result(
        m.id,
        admin.id,
        reason='Correct winner',
        corrected_scores={p.id: 2, opponent.id: 0},
        ack_critical=True,
        acknowledged_match_ids=[],
    ).unwrap()
    assert repository.get_tournament(t.id).winner_participant_id == p.id
    assert entry(party, user).group == PersonalGroup.FINISHED
    highscore = create(
        party,
        'Highscore',
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.COMPLETED,
    )
    add(highscore, user)
    tournament_service.change_status(
        highscore.id,
        TournamentStatus.ONGOING,
        admin.id,
        allow_completed_reopen=True,
    ).unwrap()
    snapshot = personal.get_personal_overview(party.id, user.id)
    assert entry_for(snapshot, highscore.id).group == PersonalGroup.ONGOING
    assert entry_for(snapshot, highscore.id).matches == []


@pytest.fixture
def site_app_for_setup(setup, make_site_app):
    party, _, _ = setup
    site_id = SiteID(str(generate_uuid7()))
    site = create_site(
        site_id,
        party.brand_id,
        server_name=f'{site_id}.test',
        party_id=party.id,
    )
    app = make_site_app(site.server_name, site.id)
    app.config['LOCALE'] = 'en'
    return app


@pytest.mark.parametrize('theme', ['standard', 'gv36'])
def test_http_personal_supervised_scope_drafts_revocation_and_nojs(
    setup, site_app_for_setup, make_user, make_party, brand, theme
):
    party, user, admin = setup
    app = site_app_for_setup
    if theme == 'gv36':
        app.jinja_env.loader = ChoiceLoader(
            [
                FileSystemLoader(
                    str(Path('sites/totalverplant-36/template_overrides'))
                ),
                app.jinja_env.loader,
            ]
        )
    own = create(
        party,
        'Own visible',
        game='Personal game',
        category=TournamentCategory.FUN,
    )
    p = add(own, user)
    add(own, admin)
    engine.generate_single_elimination_bracket(
        own.id, initiator_id=admin.id
    ).unwrap()
    other = create(party, 'Other personal secret')
    add(other, admin)
    draft = create(
        party, 'Assigned draft', tournament_status=TournamentStatus.DRAFT
    )
    foreign = create(
        make_party(brand),
        'Foreign assigned draft',
        tournament_status=TournamentStatus.DRAFT,
    )
    for t in [own, draft, foreign]:
        tournament_orga_service.assign_orga(t.id, user.id, admin.id).unwrap()
    tournament_orga_service.assign_orga(own.id, admin.id, admin.id).unwrap()
    log_in_user(user.id)
    with http_client(app, user_id=user.id) as client:
        response = client.get(
            f'/lan-tournaments/mine?user_id={admin.id}&tournament_id={other.id}&match_id=forged'
        )
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'Own visible' in html and 'Personal game' in html
        assert 'Other personal secret' not in html
        assert (
            'Assigned draft' not in html
            and 'Foreign assigned draft' not in html
        )
        assert (
            f'/lan-tournaments/matches/{entry(party, user).matches[0].match.id}'
            in html
        )
        assert user.screen_name in html and admin.screen_name in html
        assert 'tournament-list personal-list' in html
        assert 'personal-cover' not in html
        assert 'data-category-dropdown' in html
        assert ('bote-page' in html) == (theme == 'gv36')
        filtered = client.get('/lan-tournaments/mine?category=MAIN').get_data(
            as_text=True
        )
        assert 'data-personal-tournament=' not in filtered
        assert (
            client.get('/lan-tournaments/mine?category=invalid').status_code
            == 400
        )
        response = client.get('/lan-tournaments/supervised')
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'Assigned draft' in html and 'Foreign assigned draft' not in html
        assert f'href="/lan-tournaments/{draft.id}"' not in html
        assert 'Backend overview' not in html
        assert (
            client.get('/lan-tournaments/supervised?assignment=all').status_code
            == 403
        )
        assert (
            client.get(
                '/lan-tournaments/supervised?assignment=forged'
            ).status_code
            == 400
        )
        assert client.get(f'/lan-tournaments/{draft.id}').status_code == 404
        # Membership is independent of orga assignment.
        tournament_orga_service.revoke_orga(own.id, user.id, admin.id).unwrap()
        assert 'Own visible' not in client.get(
            '/lan-tournaments/supervised'
        ).get_data(as_text=True)
        assert 'Own visible' in client.get('/lan-tournaments/mine').get_data(
            as_text=True
        )
    assert repository.find_participant(p.id).removed_at is None
    with http_client(app) as client:
        assert client.get('/lan-tournaments/mine').status_code == 302
        assert client.get('/lan-tournaments/supervised').status_code == 302


def test_global_admin_own_all_and_backend_filter_contract(
    setup, site_app_for_setup, make_admin, admin_app
):
    party, user, _ = setup
    admin = make_admin(
        {
            'admin.access',
            'lan_tournament.view',
            'lan_tournament.update',
            'lan_tournament.administrate',
        }
    )
    own = create(party, 'Assigned fun', category=TournamentCategory.FUN)
    other = create(
        party, 'Not assigned main', tournament_status=TournamentStatus.DRAFT
    )
    tournament_orga_service.assign_orga(own.id, admin.id, admin.id).unwrap()
    log_in_user(admin.id)
    with http_client(site_app_for_setup, user_id=admin.id) as client:
        html = client.get('/lan-tournaments/supervised').get_data(as_text=True)
        assert 'Assigned fun' in html and 'Not assigned main' not in html
        with patch(
            'byceps.services.lan_tournament.blueprints.site.views.global_setting_service.find_setting_value',
            return_value='admin.example.test',
        ):
            html = client.get(
                '/lan-tournaments/supervised?assignment=all'
            ).get_data(as_text=True)
        assert 'Not assigned main' in html
        assert (
            f'https://admin.example.test/lan-tournaments/tournaments/{other.id}'
            in html
        )
    url = f'/lan-tournaments/for_party/{party.id}'
    with http_client(admin_app, user_id=admin.id) as client:
        html = client.get(url + '?assignment=mine&category=FUN').get_data(
            as_text=True
        )
        assert 'Assigned fun' in html and 'Not assigned main' not in html
        assert (
            'drag-handle' not in html and 'lan_tournament_sort.js' not in html
        )
        html = client.get(url + '?assignment=all&category=FUN').get_data(
            as_text=True
        )
        assert f'data-tournament-id="{other.id}"' in html
        assert 'data-tournament-count="1" hidden' in html
        assert 'lan_tournament_sort.js' in html
        assert client.get(url + '?assignment=evil').status_code == 400
        assert client.get(url + '?category=evil').status_code == 400
        assert (
            client.post(
                url + '/sort', json={'tournament_ids': [str(own.id)]}
            ).status_code
            == 400
        )
        assert (
            client.post(
                url + '/sort',
                json={'tournament_ids': [str(other.id), str(own.id)]},
            ).status_code
            == 204
        )
    tournament_orga_service.assign_orga(own.id, user.id, admin.id).unwrap()
    log_in_user(user.id)
    with http_client(admin_app, user_id=user.id) as client:
        assert client.get(url + '?assignment=mine').status_code == 403


@pytest.mark.parametrize('theme', ['standard', 'gv36'])
def test_http_group_priority_highscore_and_empty_states(
    setup, site_app_for_setup, theme
):
    party, user, admin = setup
    app = site_app_for_setup
    if theme == 'gv36':
        app.jinja_env.loader = ChoiceLoader(
            [
                FileSystemLoader(
                    str(Path('sites/totalverplant-36/template_overrides'))
                ),
                app.jinja_env.loader,
            ]
        )
    waiting = create(party, 'Waiting at position zero', position=0)
    add(waiting, user)
    playing = create(
        party,
        'Playing across categories with a very long tournament title',
        category=TournamentCategory.FUN,
        position=99,
    )
    add(playing, user)
    add(playing, admin)
    engine.generate_single_elimination_bracket(
        playing.id, initiator_id=admin.id
    ).unwrap()
    signup = create(
        party,
        'Registered early',
        position=0,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )
    add(signup, user)
    highscore = create(
        party,
        'Finished highscore',
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.COMPLETED,
    )
    add(highscore, user)
    log_in_user(user.id)
    with http_client(app, user_id=user.id) as client:
        html = client.get('/lan-tournaments/mine').get_data(as_text=True)
        assert (
            html.index(playing.name)
            < html.index(waiting.name)
            < html.index(signup.name)
            < html.index(highscore.name)
        )
        assert f'/lan-tournaments/{highscore.id}/highscore' in html
        assert f'/lan-tournaments/{highscore.id}/bracket' not in html
        assert f'/lan-tournaments/{highscore.id}/matches' not in html
        assert 'data-personal-group="ongoing"' in html
        assert 'data-personal-group="waiting"' in html
        assert 'data-personal-group="registered"' in html
        assert 'data-personal-group="finished"' in html
        assert (
            client.get('/lan-tournaments/mine?category=STAGE').status_code
            == 200
        )
        assert 'data-personal-tournament=' not in client.get(
            '/lan-tournaments/mine?category=STAGE'
        ).get_data(as_text=True)
        assert 'data-supervised-tournament=' not in client.get(
            '/lan-tournaments/supervised'
        ).get_data(as_text=True)
    log_in_user(admin.id)
    with http_client(app, user_id=admin.id) as client:
        # Being a participant in playing does not imply an orga assignment.
        assert 'data-supervised-tournament=' not in client.get(
            '/lan-tournaments/supervised'
        ).get_data(as_text=True)


@pytest.mark.parametrize('theme', ['standard', 'gv36'])
def test_http_confirmation_waiting_next_opponent_and_retraction(
    setup, site_app_for_setup, make_user, theme
):
    party, user, admin = setup
    app = site_app_for_setup
    if theme == 'gv36':
        app.jinja_env.loader = ChoiceLoader(
            [
                FileSystemLoader('sites/totalverplant-36/template_overrides'),
                app.jinja_env.loader,
            ]
        )
    t = create(party)
    own = add(t, user)
    for u in [admin, make_user(), make_user()]:
        add(t, u)
    engine.generate_single_elimination_bracket(
        t.id, initiator_id=admin.id
    ).unwrap()
    current = entry(party, user).matches[0].match
    cs = repository.get_contestants_for_match(current.id)
    # Persist both scores without confirmation, as legacy/admin data can do.
    repository.update_contestant_scores(
        {c.id: 2 if c.participant_id == own.id else 0 for c in cs}
    )
    repository.commit_session()
    assert entry(party, user).group == PersonalGroup.ONGOING
    assert entry(party, user).matches[0].match.confirmed_by is None
    log_in_user(user.id)
    with http_client(app, user_id=user.id) as client:
        before = client.get('/lan-tournaments/mine').get_data(as_text=True)
        assert 'data-personal-group="ongoing"' in before
        assert 'Go to match' in before
        engine.confirm_match(current.id, admin.id).unwrap()
        pending = entry(party, user)
        assert pending.group == PersonalGroup.WAITING
        assert pending.matches[0].match.id == current.next_match_id
        html = client.get('/lan-tournaments/mine').get_data(as_text=True)
        assert 'data-personal-group="ongoing"' not in html
        assert 'personal-match-state--waiting' in html
        assert 'Opponent pending' in html and 'View details' in html
        assert 'Go to match' not in html
        other = next(
            m
            for m in repository.get_matches_for_tournament_ordered(t.id)
            if m.id != current.id and m.next_match_id == current.next_match_id
        )
        confirm(other, admin)
        ready = entry(party, user)
        assert ready.group == PersonalGroup.ONGOING
        html = client.get('/lan-tournaments/mine').get_data(as_text=True)
        assert 'personal-match-state--open' in html
        assert f'/lan-tournaments/matches/{current.id}' not in html
        confirm(ready.matches[0].match, admin, own.id)
        assert entry(party, user).group == PersonalGroup.FINISHED
        html = client.get('/lan-tournaments/mine').get_data(as_text=True)
        assert 'personal-match-state--confirmed' in html
        assert 'Go to match' not in html
        engine.correct_match_result(
            ready.matches[0].match.id,
            admin.id,
            reason='Reopen final',
            corrected_scores=None,
            ack_critical=True,
            acknowledged_match_ids=[],
        ).unwrap()
        assert entry(party, user).group == PersonalGroup.ONGOING
        assert 'Go to match' in client.get('/lan-tournaments/mine').get_data(
            as_text=True
        )


@pytest.mark.parametrize('theme', ['standard', 'gv36'])
def test_navigation_requires_current_party_assignment_and_refreshes_on_revoke(
    setup, site_app_for_setup, make_admin, make_party, brand, theme
):
    party, user, initiator = setup
    app = site_app_for_setup
    if theme == 'gv36':
        app.jinja_env.loader = ChoiceLoader(
            [
                FileSystemLoader('sites/totalverplant-36/template_overrides'),
                app.jinja_env.loader,
            ]
        )
    draft = create(party, 'Nav draft', tournament_status=TournamentStatus.DRAFT)
    visible = create(party, 'Participant only')
    add(visible, user)
    foreign = create(make_party(brand), 'Foreign assignment')
    tournament_orga_service.assign_orga(
        foreign.id, user.id, initiator.id
    ).unwrap()
    log_in_user(user.id)
    with http_client(app, user_id=user.id) as client:
        for path in [
            '/lan-tournaments/',
            '/lan-tournaments/mine',
            '/lan-tournaments/supervised',
        ]:
            assert not _supervised_nav_visible(client.get(path))
        tournament_orga_service.assign_orga(
            draft.id, user.id, initiator.id
        ).unwrap()
        for path in [
            '/lan-tournaments/?category=FUN',
            '/lan-tournaments/mine?category=FUN',
            '/lan-tournaments/supervised?category=FUN',
        ]:
            assert _supervised_nav_visible(client.get(path))
        tournament_orga_service.revoke_orga(
            draft.id, user.id, initiator.id
        ).unwrap()
        response = client.get('/lan-tournaments/supervised')
        assert response.status_code == 200
        assert not _supervised_nav_visible(response)
        assert response.get_data(as_text=True).count('no-data-message') == 1
        assert 'data-category-dropdown' not in response.get_data(as_text=True)
    admin = make_admin({'lan_tournament.administrate'})
    log_in_user(admin.id)
    with http_client(app, user_id=admin.id) as client:
        assert not _supervised_nav_visible(client.get('/lan-tournaments/'))
        response = client.get('/lan-tournaments/supervised?assignment=all')
        assert response.status_code == 200
        assert not _supervised_nav_visible(response)
        assert 'Nav draft' in response.get_data(as_text=True)
    with http_client(app) as client:
        assert not _supervised_nav_visible(client.get('/lan-tournaments/'))


def _supervised_nav_visible(response):
    html = response.get_data(as_text=True)
    nav = html.split('tournament-overview-nav', 1)[1].split('</nav>', 1)[0]
    return '/lan-tournaments/supervised' in nav


def highscore(party, **kwargs):
    return create(
        party,
        **{
            'game_format': GameFormat.HIGHSCORE,
            'elimination_mode': EliminationMode.NONE,
            'score_ordering': ScoreOrdering.HIGHER_IS_BETTER,
            **kwargs,
        },
    )


def submit(t, *, participant_id=None, team_id=None, score=0, official=True):
    submission = DbScoreSubmission(
        generate_uuid7(),
        t.id,
        score,
        datetime.now(UTC),
        participant_id=participant_id,
        team_id=team_id,
        is_official=official,
    )
    db.session.add(submission)
    db.session.commit()
    return submission.id


def test_highscore_current_solo_coverage_and_reload(
    setup, make_user, make_party, brand
):
    party, user, other = setup
    t = highscore(party)
    own, opponent = add(t, user), add(t, other)
    assert not entry(party, user).awaiting_completion
    submit(t, participant_id=own.id, score=0)
    submit(t, participant_id=own.id, score=42)
    submit(t, participant_id=opponent.id, official=False)
    submit(t, participant_id=opponent.id, score=-1)
    foreign = highscore(make_party(brand))
    submit(foreign, participant_id=opponent.id)
    assert not entry(party, user).awaiting_completion
    submission_id = submit(t, participant_id=opponent.id)
    snapshot = entry(party, user)
    assert (
        snapshot.awaiting_completion and snapshot.group == PersonalGroup.WAITING
    )
    assert not snapshot.needs_attention
    assert (
        repository.get_tournament(t.id).tournament_status
        == TournamentStatus.ONGOING
    )
    db.session.execute(
        delete(DbScoreSubmission).where(DbScoreSubmission.id == submission_id)
    )
    db.session.commit()
    assert not entry(party, user).awaiting_completion
    submit(t, participant_id=opponent.id)
    added = add(t, make_user())
    assert not entry(party, user).awaiting_completion
    db.session.execute(
        update(DbTournamentParticipant)
        .where(DbTournamentParticipant.id == added.id)
        .values(removed_at=datetime.now(UTC))
    )
    db.session.commit()
    assert entry(party, user).awaiting_completion
    tournament_service.change_status(
        t.id, TournamentStatus.COMPLETED, user.id
    ).unwrap()
    assert entry(party, user).group == PersonalGroup.FINISHED
    assert not entry(party, user).awaiting_completion


def test_highscore_team_coverage_counts_current_teams_once(setup, make_user):
    party, user, other = setup
    t = highscore(party, contestant_type=ContestantType.TEAM)
    teams = []
    for index, members in enumerate([[user, make_user()], [other]]):
        team = TournamentTeam(
            id=generate_uuid7(),
            tournament_id=t.id,
            name=f'Team {index}',
            tag=None,
            description=None,
            image_url=None,
            captain_user_id=members[0].id,
            join_code=None,
            created_at=datetime.now(UTC),
        )
        repository.create_team(team)
        for member in members:
            p = add(t, member)
            repository.update_participant(replace(p, team_id=team.id))
        teams.append(team)
    submit(t, team_id=teams[0].id)
    submit(t, team_id=teams[0].id, score=9)
    assert not entry(party, user).awaiting_completion
    opponent = repository.find_participant_by_user(t.id, other.id)
    # A solo submission cannot cover a team, even if its member submitted it.
    submit(t, participant_id=opponent.id)
    assert not entry(party, user).awaiting_completion
    submit(t, team_id=teams[1].id)
    assert entry(party, user).awaiting_completion
    empty_team = replace(teams[0], id=generate_uuid7(), name='No participation')
    repository.create_team(empty_team)
    removed_team = replace(teams[0], id=generate_uuid7(), name='Removed team')
    repository.create_team(removed_team)
    p = add(t, make_user())
    repository.update_participant(replace(p, team_id=removed_team.id))
    db.session.execute(
        update(DbTournamentTeam)
        .where(DbTournamentTeam.id == removed_team.id)
        .values(removed_at=datetime.now(UTC))
    )
    db.session.commit()
    assert entry(party, user).awaiting_completion
    repository.update_participant(replace(opponent, team_id=empty_team.id))
    assert not entry(party, user).awaiting_completion
    db.session.execute(
        update(DbTournamentParticipant)
        .where(DbTournamentParticipant.id == opponent.id)
        .values(removed_at=datetime.now(UTC))
    )
    db.session.commit()
    assert entry(party, user).awaiting_completion


def test_highscore_coverage_is_batched_scoped_nonempty_and_readonly(
    setup, make_party, brand
):
    party, user, _ = setup
    empty = highscore(party)
    types = {empty.id: ContestantType.SOLO}
    with queries() as statements:
        assert (
            tournament_personal_repository.get_highscores_with_complete_results(
                party.id, types
            )
            == set()
        )
    assert len(statements) == 2
    with queries() as statements:
        assert (
            tournament_personal_repository.get_highscores_with_complete_results(
                party.id, {}
            )
            == set()
        )
    assert statements == []
    for i in range(5):
        t = highscore(party, name=f'Coverage {i}')
        p = add(t, user)
        submit(t, participant_id=p.id)
        types[t.id] = ContestantType.SOLO
        with queries() as statements:
            context = personal.get_personal_overview(party.id, user.id)
        if i == 0:
            query_count = len(statements)
        assert len(statements) == query_count
        assert all(
            sql.lstrip().upper().startswith('SELECT') for sql in statements
        )
        assert all(e.awaiting_completion for e in context['entries'])
    assert (
        tournament_personal_repository.get_highscores_with_complete_results(
            make_party(brand).id, types
        )
        == set()
    )


@pytest.mark.parametrize('theme', ['standard', 'gv36'])
def test_http_complete_highscore_waiting_then_submission_change(
    setup, site_app_for_setup, theme
):
    party, user, other = setup
    app = site_app_for_setup
    if theme == 'gv36':
        app.jinja_env.loader = ChoiceLoader(
            [
                FileSystemLoader('sites/totalverplant-36/template_overrides'),
                app.jinja_env.loader,
            ]
        )
    t = highscore(party)
    own, opponent = add(t, user), add(t, other)
    submit(t, participant_id=own.id)
    submit(t, participant_id=opponent.id)
    log_in_user(user.id)
    with http_client(app, user_id=user.id) as client:
        html = client.get('/lan-tournaments/mine').get_data(as_text=True)
        assert 'data-personal-group="waiting"' in html
        assert (
            'Waiting for the tournament orga to complete the tournament' in html
        )
        assert 'Submit a score / Leaderboard' not in html
        assert f'/lan-tournaments/{t.id}/highscore' in html
        assert 'class="personal-footer"' not in html
        db.session.execute(
            update(DbScoreSubmission)
            .where(DbScoreSubmission.participant_id == opponent.id)
            .values(is_official=False)
        )
        db.session.commit()
        html = client.get('/lan-tournaments/mine').get_data(as_text=True)
        assert 'data-personal-group="ongoing"' in html
        assert 'Submit a score / Leaderboard' in html


@pytest.mark.parametrize('theme', ['standard', 'gv36'])
def test_get_category_links_preserve_stock_and_assignment_scope(
    setup, site_app_for_setup, make_admin, theme
):
    party, user, initiator = setup
    app = site_app_for_setup
    if theme == 'gv36':
        app.jinja_env.loader = ChoiceLoader(
            [
                FileSystemLoader('sites/totalverplant-36/template_overrides'),
                app.jinja_env.loader,
            ]
        )
    main = create(party, 'Main entry')
    fun = create(party, 'Fun entry', category=TournamentCategory.FUN)
    for t in [main, fun]:
        add(t, user)
        tournament_orga_service.assign_orga(
            t.id, user.id, initiator.id
        ).unwrap()
    log_in_user(user.id)
    with http_client(app, user_id=user.id) as client:
        for path in [
            '/lan-tournaments/',
            '/lan-tournaments/mine',
            '/lan-tournaments/supervised',
        ]:
            response = client.get(path + '?category=FUN')
            assert response.status_code == 200
            html = response.get_data(as_text=True)
            assert 'Main entry' not in html and 'Fun entry' in html
            options = html.split(
                '<div class="tournament-category-options">', 1
            )[1].split('</div>', 1)[0]
            assert 'category=MAIN' in options and 'category=FUN' in options
            assert 'category=STAGE' not in options
            if path.endswith('supervised'):
                assert options.count('assignment=mine') == 3
            empty = client.get(path + '?category=STAGE').get_data(as_text=True)
            assert empty.count('data-tournament-filter-empty') == 1
            assert 'category=ALL' in empty
            assert 'Stage / Offline' in empty
            assert client.get(path + '?category=invalid').status_code == 400


def test_admin_supervised_tab_on_layout_pages_and_empty_filters(
    setup, make_admin, admin_app
):
    party, _, _ = setup
    admin = make_admin(
        {
            'admin.access',
            'lan_tournament.view',
            'lan_tournament.update',
            'lan_tournament.request_view',
            'lan_tournament.maintain',
        }
    )
    t = create(party, 'Supervised fun', category=TournamentCategory.FUN)
    tournament_orga_service.assign_orga(t.id, admin.id, admin.id).unwrap()
    log_in_user(admin.id)
    root = f'/lan-tournaments/for_party/{party.id}'
    with http_client(admin_app, user_id=admin.id) as client:
        for path in [
            root,
            root + '/overview',
            root + '/requests',
            root + '/maintenance',
            f'/lan-tournaments/tournaments/{t.id}',
        ]:
            response = client.get(path)
            assert response.status_code == 200
            assert f'href="{root}?assignment=mine"' in response.get_data(
                as_text=True
            )
        html = client.get(root + '?assignment=mine&category=MAIN').get_data(
            as_text=True
        )
        assert html.count('data-tournament-filter-empty') == 1
        assert 'data-tournament-category="MAIN"' not in html
        assert (
            'drag-handle' not in html and 'lan_tournament_sort.js' not in html
        )
        assert 'data-overview-filters' not in html
        assert (
            client.get(root + '/overview?category=invalid').status_code == 400
        )
        tournament_orga_service.revoke_orga(t.id, admin.id, admin.id).unwrap()
        html = client.get(root + '?assignment=mine').get_data(as_text=True)
        assert html.count('no-data-message') == 1
        assert f'href="{root}?assignment=mine"' not in html
