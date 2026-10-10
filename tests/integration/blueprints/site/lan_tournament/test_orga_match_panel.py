"""
tests.integration.blueprints.site.lan_tournament.test_orga_match_panel
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from html.parser import HTMLParser

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_orga_service,
    tournament_participant_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.tournament_match_comment import (
    MatchCommentContext,
)
from byceps.services.ticketing import ticket_creation_service

from tests.helpers import generate_token, http_client, log_in_user


BASE_URL = 'http://www.acmecon.test/lan-tournaments'
COMMENT_FORM_ID = 'lt-match-comment-form'


@pytest.fixture(scope='module')
def players(make_user, make_ticket_category, party):
    category = make_ticket_category(party.id, 'Orga Panel Entry')
    users = [make_user(f'OrgaPanelPlayer{i}') for i in range(4)]
    for user in users:
        ticket_creation_service.create_ticket(category, user, user=user)
    return users


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('OrgaPanelOrga')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def bracket(party, players, orga):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        'Orga panel',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).unwrap()
    for player in players:
        tournament_participant_service.join_tournament(
            tournament.id, player.id
        ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).unwrap()
    tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).unwrap()
    tournament_orga_service.assign_orga(
        tournament.id, orga.id, orga.id
    ).unwrap()

    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    played, open_ = (
        m
        for m in matches
        if m.bracket in (None, Bracket.WINNERS) and m.round == 0
    )
    contestants = tournament_match_service.get_contestants_for_match(played.id)
    tournament_match_service.admin_set_and_confirm_match(
        played.id,
        orga.id,
        {contestants[0].participant_id: 3, contestants[1].participant_id: 1},
    ).unwrap()

    return tournament, played, open_


def _get(app, path, user=None):
    with http_client(app, user_id=user.id if user else None) as client:
        return client.get(f'{BASE_URL}{path}')


def _post_flashed(app, user, path, **data):
    with http_client(app, user_id=user.id) as client:
        response = client.post(f'{BASE_URL}{path}', data=data)
        with client.session_transaction() as session:
            flashes = session.get('_flashes', [])
    return response, [(f['category'], f['text']) for _, f in flashes]


class _FormMap(HTMLParser):
    """Collect the forms, buttons and result controls of a rendered page."""

    def __init__(self):
        super().__init__()
        self.form_ids = []
        self.buttons = []  # (attributes, id of the enclosing form)
        self.textareas = []
        self.result_controls = []
        self._open_forms = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        enclosing = self._open_forms[-1] if self._open_forms else None
        if tag == 'form':
            self.form_ids.append(attrs.get('id'))
            self._open_forms.append(attrs.get('id'))
        elif tag == 'button':
            self.buttons.append((attrs, enclosing))
        elif tag == 'textarea':
            self.textareas.append((attrs, enclosing))
        elif tag in ('input', 'select') and (
            attrs.get('name') or ''
        ).startswith(('score_', 'placement_')):
            self.result_controls.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'form' and self._open_forms:
            self._open_forms.pop()

    def confirm_buttons(self):
        return [(a, f) for a, f in self.buttons if 'formaction' in a]


def _form_map(response):
    parsed = _FormMap()
    parsed.feed(response.get_data(as_text=True))
    return parsed


def _assert_results_belong_to_the_comment_form(
    parsed, *, control_count, formaction_suffix
):
    assert parsed.form_ids.count(COMMENT_FORM_ID) == 1
    assert len(parsed.result_controls) == control_count
    assert all(c.get('form') == COMMENT_FORM_ID for c in parsed.result_controls)

    ((confirm, enclosing),) = parsed.confirm_buttons()
    assert enclosing == COMMENT_FORM_ID
    assert confirm['formaction'].endswith(formaction_suffix)

    post_buttons = [
        a
        for a, f in parsed.buttons
        if f == COMMENT_FORM_ID and 'formaction' not in a
    ]
    assert len(post_buttons) == 1
    assert 'formnovalidate' in post_buttons[0]

    # Enter in a result control submits the first button of the form.
    form_buttons = [a for a, f in parsed.buttons if f == COMMENT_FORM_ID]
    assert form_buttons.index(confirm) < form_buttons.index(post_buttons[0])

    ((textarea, enclosing),) = parsed.textareas
    assert enclosing == COMMENT_FORM_ID
    assert 'required' in textarea
    assert textarea['maxlength'] == '1000'


def test_orga_gets_correction_but_no_unconfirm_on_bracket_match(
    site_app, bracket, orga
):
    _, played, _ = bracket

    response = _get(site_app, f'/matches/{played.id}', orga)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'orga_correction_reason' in html
    assert 'orga_unconfirm_reason' not in html


def test_orga_gets_confirm_form_on_open_match(site_app, bracket, orga):
    _, _, open_ = bracket

    response = _get(site_app, f'/matches/{open_.id}', orga)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'orga_score_' in html
    assert COMMENT_FORM_ID in html
    assert f'/orga/matches/{open_.id}/confirm_with_scores' in html


def test_orga_result_controls_bind_to_the_one_comment_form(
    site_app, bracket, orga
):
    _, _, open_ = bracket

    response = _get(site_app, f'/matches/{open_.id}', orga)

    assert response.status_code == 200
    _assert_results_belong_to_the_comment_form(
        _form_map(response),
        control_count=2,
        formaction_suffix=f'/orga/matches/{open_.id}/confirm_with_scores',
    )


def test_contestant_sees_no_confirm_button(site_app, bracket, players):
    _, _, open_ = bracket
    participant_id = tournament_match_service.get_contestants_for_match(
        open_.id
    )[0].participant_id
    participant = tournament_participant_service.get_participant(participant_id)
    contestant = next(p for p in players if p.id == participant.user_id)
    log_in_user(contestant.id)

    response = _get(site_app, f'/matches/{open_.id}', contestant)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    parsed = _form_map(response)
    assert parsed.confirm_buttons() == []
    assert parsed.result_controls == []
    assert 'orga_score_' not in html
    assert 'confirm_with_scores' not in html
    assert parsed.form_ids.count(COMMENT_FORM_ID) == 1


def test_anonymous_visitor_gets_no_orga_panel(site_app, bracket):
    _, played, _ = bracket

    response = _get(site_app, f'/matches/{played.id}')

    assert response.status_code == 200
    assert 'orga_correction_reason' not in response.get_data(as_text=True)


def test_paused_tournament_locks_the_result_forms(site_app, bracket, orga):
    tournament, played, _ = bracket
    tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED
    ).unwrap()
    try:
        response = _get(site_app, f'/matches/{played.id}', orga)
    finally:
        tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING
        ).unwrap()

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'orga_correction_reason' not in html
    assert 'orga_comment' not in html
    assert COMMENT_FORM_ID in html
    assert 'confirm_with_scores' not in html


def test_orga_unconfirm_of_bracket_match_changes_nothing(
    site_app, bracket, orga
):
    _, played, _ = bracket

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(
            f'{BASE_URL}/orga/matches/{played.id}/unconfirm',
            data={'reason': 'posted directly'},
        )

    assert response.status_code == 302
    match = tournament_match_service.get_match(played.id)
    assert match.confirmed_by is not None


@pytest.fixture
def open_match(party, players, orga):
    """Provide an unconfirmed match of a tournament of its own."""
    tournament, _ = tournament_service.create_tournament(
        party.id,
        f'Orga panel confirm {generate_token()}',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).unwrap()
    for player in players:
        tournament_participant_service.join_tournament(
            tournament.id, player.id
        ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).unwrap()
    tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).unwrap()
    tournament_orga_service.assign_orga(
        tournament.id, orga.id, orga.id
    ).unwrap()

    match = next(
        m
        for m in tournament_match_service.get_matches_for_tournament_ordered(
            tournament.id
        )
        if m.bracket in (None, Bracket.WINNERS) and m.round == 0
    )
    contestants = tournament_match_service.get_contestants_for_match(match.id)
    return match, contestants


def _scores(contestants):
    return {
        f'score_{contestants[0].participant_id}': '3',
        f'score_{contestants[1].participant_id}': '1',
    }


def test_orga_confirm_post_without_comment_is_refused(
    site_app, open_match, orga
):
    match, contestants = open_match

    response, flashes = _post_flashed(
        site_app,
        orga,
        f'/orga/matches/{match.id}/confirm_with_scores',
        **_scores(contestants),
    )

    assert response.status_code == 302
    assert [category for category, _ in flashes] == ['danger']
    assert 'A comment is required to confirm the match.' in flashes[0][1]
    assert tournament_match_service.get_match(match.id).confirmed_by is None
    assert tournament_match_service.get_comments_from_match(match.id) == []


def test_orga_confirm_post_with_comment_confirms_and_marks(
    site_app, open_match, orga
):
    match, contestants = open_match

    response, flashes = _post_flashed(
        site_app,
        orga,
        f'/orga/matches/{match.id}/confirm_with_scores',
        comment='Result checked with the referee',
        **_scores(contestants),
    )

    assert response.status_code == 302
    assert [category for category, _ in flashes] == ['success']
    assert tournament_match_service.get_match(match.id).confirmed_by == orga.id
    (comment,) = tournament_match_service.get_comments_from_match(match.id)
    assert comment.comment == 'Result checked with the referee'
    assert comment.context == MatchCommentContext.ORGA_CONFIRMATION

    page = _get(site_app, f'/matches/{match.id}', orga)

    html = page.get_data(as_text=True)
    assert 'Result checked with the referee' in html
    assert html.count('Match confirmation by the tournament orga') == 1


@pytest.fixture(scope='module')
def ffa(party, players, orga):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        'Orga panel FFA',
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
        group_size_min=2,
        group_size_max=2,
        advancement_count=1,
        point_table=[3, 1],
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).unwrap()
    for player in players:
        tournament_participant_service.join_tournament(
            tournament.id, player.id
        ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).unwrap()
    tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=orga.id
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).unwrap()
    tournament_orga_service.assign_orga(
        tournament.id, orga.id, orga.id
    ).unwrap()

    played, open_ = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    first, second = tournament_match_service.get_contestants_for_match(
        played.id
    )
    tournament_match_service.set_ffa_placements(
        played.id,
        {str(first.participant_id): 1, str(second.participant_id): 2},
    ).unwrap()
    tournament_match_service.confirm_ffa_match(played.id, orga.id).unwrap()

    return played, open_, first.participant_id, second.participant_id


def test_orga_gets_placement_form_on_open_ffa_match(site_app, ffa, orga):
    _, open_, _, _ = ffa

    response = _get(site_app, f'/matches/{open_.id}', orga)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'/orga/matches/{open_.id}/submit_ffa_result' in html
    assert 'orga_placement_' in html
    _assert_results_belong_to_the_comment_form(
        _form_map(response),
        control_count=2,
        formaction_suffix=f'/orga/matches/{open_.id}/submit_ffa_result',
    )


def test_orga_can_reenter_and_confirm_an_unconfirmed_ffa_result(
    site_app, ffa, orga
):
    played, _, winner_id, loser_id = ffa

    with http_client(site_app, user_id=orga.id) as client:
        client.post(
            f'{BASE_URL}/orga/matches/{played.id}/unconfirm',
            data={'reason': 'placements swapped'},
        )
        response = client.post(
            f'{BASE_URL}/orga/matches/{played.id}/submit_ffa_result',
            data={
                f'placement_{winner_id}': '2',
                f'placement_{loser_id}': '1',
                'comment': 'Placements corrected',
            },
        )

    assert response.status_code == 302
    match = tournament_match_service.get_match(played.id)
    assert match.confirmed_by == orga.id
    placements = {
        c.participant_id: c.placement
        for c in tournament_match_service.get_contestants_for_match(played.id)
    }
    assert placements == {winner_id: 2, loser_id: 1}
