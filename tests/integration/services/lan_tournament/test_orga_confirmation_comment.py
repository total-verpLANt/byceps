"""
tests.integration.services.lan_tournament.test_orga_confirmation_comment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An orga confirmation and its comment commit together or not at all.
"""

import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_match_service,
    tournament_participant_service,
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_match_comment import (
    MatchCommentContext,
)
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-2026-orga-confirmation-comment')

BLANK_ERROR = 'A comment is required to confirm the match.'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Orga Confirmation')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Orga Confirmation Entry')


@pytest.fixture(scope='module')
def ticketed(make_user, ticket_category):
    users = [make_user(f'OrgaConfirm{i:02d}') for i in range(4)]
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('OrgaConfirmAdmin')


def _started(name, ticketed, **kwargs):
    result = tournament_service.create_tournament(
        PARTY_ID, name, contestant_type=ContestantType.SOLO, **kwargs
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).is_ok()
    for user in ticketed:
        assert tournament_participant_service.join_tournament(
            tournament.id, user.id
        ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).is_ok()
    return tournament


def _se_match(name, ticketed, admin):
    """Return an ongoing SE tournament, a semifinal and its scores."""
    tournament = _started(
        name,
        ticketed,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        max_players=8,
    )
    assert tournament_match_service.generate_single_elimination_bracket(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).is_ok()
    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    semi = next(m for m in matches if m.round == 0 and m.bracket is None)
    a, b = tournament_match_service.get_contestants_for_match(semi.id)
    return tournament, semi, {a.participant_id: 7, b.participant_id: 2}


def _ffa_match(name, ticketed, admin):
    """Return an ongoing FFA tournament, its only match and placements."""
    tournament = _started(
        name,
        ticketed,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        max_players=8,
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
        point_table=[10, 6, 3, 1],
    )
    assert tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).is_ok()
    (match,) = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    contestants = tournament_match_service.get_contestants_for_match(match.id)
    placements = {
        str(c.participant_id): i + 1 for i, c in enumerate(contestants)
    }
    return tournament, match, placements


def _committed_comments(match_id):
    """Read the comments through a connection of its own: only commits."""
    with db.engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    'SELECT id, created_by, comment, context'
                    ' FROM lan_tournament_match_comments'
                    ' WHERE tournament_match_id = :match_id'
                    ' ORDER BY created_at'
                ),
                {'match_id': match_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _is_confirmed(match_id):
    return tournament_match_service.get_match(match_id).confirmed_by is not None


def _entries(tournament_id, event_type):
    return [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament_id
        )
        if e.event_type == event_type
    ]


def test_confirm_with_comment_stores_marked_comment(party, ticketed, admin):
    _, semi, scores = _se_match('Confirm comment stored', ticketed, admin)

    result = tournament_match_service.admin_set_and_confirm_match(
        semi.id,
        admin.id,
        scores,
        confirmation_comment='  Ergebnis per Screenshot  ',
    )

    assert result.is_ok(), result.unwrap_err()
    assert tournament_match_service.get_match(semi.id).confirmed_by == admin.id
    (stored,) = _committed_comments(semi.id)
    assert stored['comment'] == 'Ergebnis per Screenshot'
    assert stored['context'] == 'orga_confirmation'
    assert stored['created_by'] == admin.id
    (comment,) = tournament_match_service.get_comments_from_match(semi.id)
    assert comment.context is MatchCommentContext.ORGA_CONFIRMATION


def test_refused_confirm_leaves_no_comment(party, ticketed, admin, monkeypatch):
    _, semi, scores = _se_match('Refused confirm', ticketed, admin)
    tie = dict.fromkeys(scores, 5)
    rollbacks = []
    real_rollback = tournament_repository.rollback_session

    def spy_rollback():
        rollbacks.append(True)
        real_rollback()

    monkeypatch.setattr(tournament_repository, 'rollback_session', spy_rollback)

    result = tournament_match_service.admin_set_and_confirm_match(
        semi.id, admin.id, tie, confirmation_comment='Unentschieden'
    )

    assert result.is_err()
    assert rollbacks == [True]
    assert _committed_comments(semi.id) == []
    assert not _is_confirmed(semi.id)


def test_failed_commit_rolls_back_the_staged_comment(
    party, ticketed, admin, monkeypatch
):
    tournament, semi, scores = _se_match('Failed commit', ticketed, admin)

    staged_at_commit = []

    def failing_commit():
        staged_at_commit.append(
            len(tournament_match_service.get_comments_from_match(semi.id))
        )
        raise RuntimeError('commit failed')

    with monkeypatch.context() as patched:
        patched.setattr(tournament_repository, 'commit_session', failing_commit)
        with pytest.raises(RuntimeError, match='commit failed'):
            tournament_match_service.admin_set_and_confirm_match(
                semi.id,
                admin.id,
                scores,
                confirmation_comment='Nie gespeichert',
            )

    assert staged_at_commit == [1]
    assert _committed_comments(semi.id) == []
    assert not _is_confirmed(semi.id)
    assert _entries(tournament.id, 'match-result-entered') == []


def test_blank_comment_refuses_confirm(party, ticketed, admin):
    _, semi, scores = _se_match('Blank comment', ticketed, admin)

    result = tournament_match_service.admin_set_and_confirm_match(
        semi.id, admin.id, scores, confirmation_comment='  '
    )

    assert result.is_err()
    assert result.unwrap_err() == BLANK_ERROR
    assert _committed_comments(semi.id) == []
    assert not _is_confirmed(semi.id)


def test_confirm_without_kwarg_stores_no_comment(party, ticketed, admin):
    tournament, semi, scores = _se_match('No kwarg', ticketed, admin)

    result = tournament_match_service.admin_set_and_confirm_match(
        semi.id, admin.id, scores
    )

    assert result.is_ok(), result.unwrap_err()
    assert _is_confirmed(semi.id)
    assert _committed_comments(semi.id) == []
    (entry,) = _entries(tournament.id, 'match-result-entered')
    assert 'comment_id' not in entry.data


def test_set_and_confirm_ffa_with_comment_stores_marked_comment(
    party, ticketed, admin
):
    _, match, placements = _ffa_match('FFA set and confirm', ticketed, admin)

    result = tournament_match_service.set_and_confirm_ffa_match(
        match.id,
        placements,
        admin.id,
        confirmation_comment=' Lobby-Screenshot geprueft ',
    )

    assert result.is_ok(), result.unwrap_err()
    assert tournament_match_service.get_match(match.id).confirmed_by == admin.id
    (stored,) = _committed_comments(match.id)
    assert stored['comment'] == 'Lobby-Screenshot geprueft'
    assert stored['context'] == 'orga_confirmation'
    assert stored['created_by'] == admin.id


def test_confirm_ffa_match_with_comment_stores_marked_comment(
    party, ticketed, admin
):
    _, match, placements = _ffa_match('FFA confirm', ticketed, admin)
    assert tournament_match_service.set_ffa_placements(
        match.id, placements
    ).is_ok()

    result = tournament_match_service.confirm_ffa_match(
        match.id, admin.id, confirmation_comment='Platzierungen stimmen'
    )

    assert result.is_ok(), result.unwrap_err()
    assert tournament_match_service.get_match(match.id).confirmed_by == admin.id
    (stored,) = _committed_comments(match.id)
    assert stored['comment'] == 'Platzierungen stimmen'
    assert stored['context'] == 'orga_confirmation'
    assert stored['created_by'] == admin.id


def test_refused_ffa_confirm_leaves_no_comment(party, ticketed, admin):
    _, match, _ = _ffa_match('FFA refused confirm', ticketed, admin)

    result = tournament_match_service.confirm_ffa_match(
        match.id, admin.id, confirmation_comment='Platzierungen fehlen'
    )

    assert result.is_err()
    assert _committed_comments(match.id) == []
    assert not _is_confirmed(match.id)


def test_confirm_log_entry_references_comment_id(party, ticketed, admin):
    tournament, semi, scores = _se_match('Log comment id', ticketed, admin)

    result = tournament_match_service.admin_set_and_confirm_match(
        semi.id, admin.id, scores, confirmation_comment='Siehe Screenshot'
    )

    assert result.is_ok(), result.unwrap_err()
    (stored,) = _committed_comments(semi.id)
    (entry,) = _entries(tournament.id, 'match-result-entered')
    assert entry.data['comment_id'] == str(stored['id'])


def test_plain_comment_has_no_context(party, ticketed, admin):
    _, semi, _ = _se_match('Plain comment', ticketed, admin)

    result = tournament_match_service.add_comment(
        semi.id, ticketed[0].id, 'Viel Erfolg!'
    )

    assert result.is_ok(), result.unwrap_err()
    (stored,) = _committed_comments(semi.id)
    assert stored['context'] is None
    (comment,) = tournament_match_service.get_comments_from_match(semi.id)
    assert comment.context is None


ADMIN_URL = 'http://admin.acmecon.test/lan-tournaments'

COMMENT_FORM_ID = 'lt-admin-match-comment-form'

# The test environment renders either the msgid or its German form.
MARKER_TEXTS = (
    'Match confirmation by the tournament orga',
    'Partie-Bestätigung durch Turnier-Orga',
)
BLANK_TEXTS = (
    BLANK_ERROR,
    'Zum Bestätigen der Partie ist ein Kommentar erforderlich.',
)


@pytest.fixture(scope='module')
def clients(make_admin, make_client, admin_app):
    """Provide an admin client per permission set, as (user, client)."""
    from tests.helpers import log_in_user

    permission_sets = {
        'full': {'lan_tournament.administrate', 'lan_tournament.update'},
        'confirm_only': {'lan_tournament.administrate'},
        'comment_only': {'lan_tournament.update'},
    }
    clients = {}
    for name, permissions in permission_sets.items():
        user = make_admin(
            {'admin.access', 'lan_tournament.view', *permissions},
            f'AdminConfirmPage{name.title().replace("_", "")}',
        )
        log_in_user(user.id)
        clients[name] = (user, make_client(admin_app, user_id=user.id))
    return clients


def _match_page(client, match_id):
    response = client.get(f'{ADMIN_URL}/matches/{match_id}')
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _flashes(client):
    with client.session_transaction() as session:
        flashes = session.pop('_flashes', None) or []
    return ' | '.join(str(message) for _, message in flashes)


def _forms(html):
    """Return the opening tag and the body of every form on the page."""
    import re

    return [
        (match.group(1), match.group(2))
        for match in re.finditer(
            r'<form\b([^>]*)>(.*?)</form>', html, re.DOTALL
        )
    ]


def _comment_form(html):
    """Return the comment form's opening tag and body."""
    (form,) = [
        (opening, body)
        for opening, body in _forms(html)
        if f"id='{COMMENT_FORM_ID}'" in opening
    ]
    return form


def _forms_with_formaction(html, route):
    """Return the forms that hold a button with a `formaction` to `route`."""
    import re

    return [
        opening
        for opening, body in _forms(html)
        if re.search(rf"formaction='[^']*/{route}'", body)
    ]


def _forms_posting_to(html, route):
    """Return the forms whose own `action` is `route`."""
    import re

    return [
        opening
        for opening, _ in _forms(html)
        if re.search(rf"action='[^']*/{route}'", opening)
    ]


def _score_inputs(html):
    import re

    return re.findall(r"<input[^>]*name='score_[^>]*>", html)


def _confirmed_now(match_id):
    db.session.rollback()
    return _is_confirmed(match_id)


def test_admin_match_page_confirm_requires_comment(
    party, ticketed, admin, clients
):
    _, semi, scores = _se_match('Admin page confirm', ticketed, admin)
    user, client = clients['full']
    url = f'{ADMIN_URL}/matches/{semi.id}/confirm_with_scores'
    form_scores = {f'score_{key}': str(value) for key, value in scores.items()}

    for blank in ({}, {'comment': '   '}):
        response = client.post(url, data=form_scores | blank)

        assert response.status_code == 302
        flashes = _flashes(client)
        assert any(text in flashes for text in BLANK_TEXTS), flashes
        assert not _confirmed_now(semi.id)
        assert _committed_comments(semi.id) == []

    response = client.post(
        url, data=form_scores | {'comment': '  Ergebnis per Screenshot  '}
    )

    assert response.status_code == 302
    assert _confirmed_now(semi.id)
    (stored,) = _committed_comments(semi.id)
    assert stored['comment'] == 'Ergebnis per Screenshot'
    assert stored['created_by'] == user.id
    (comment,) = tournament_match_service.get_comments_from_match(semi.id)
    assert comment.context is MatchCommentContext.ORGA_CONFIRMATION
    html = _match_page(client, semi.id)
    assert any(marker in html for marker in MARKER_TEXTS)
    assert 'Ergebnis per Screenshot' in html


def test_admin_match_page_confirm_button_sits_in_comment_form(
    party, ticketed, admin, clients
):
    import re

    _, semi, scores = _se_match('Admin page form', ticketed, admin)
    _, client = clients['full']

    html = _match_page(client, semi.id)

    opening, body = _comment_form(html)
    assert f"/matches/{semi.id}/add_comment'" in opening
    assert re.search(
        rf"formaction='[^']*/matches/{semi.id}/confirm_with_scores'", body
    )
    assert 'formnovalidate' in body
    assert re.search(r"<textarea[^>]*name='comment'[^>]*\brequired\b", body)
    assert _forms_posting_to(html, 'confirm_with_scores') == []
    inputs = _score_inputs(html)
    assert len(inputs) == len(scores)
    assert all(f"form='{COMMENT_FORM_ID}'" in tag for tag in inputs)
    assert not any(marker in html for marker in MARKER_TEXTS)


def test_admin_match_page_confirmed_match_offers_no_confirm_button(
    party, ticketed, admin, clients
):
    _, semi, scores = _se_match('Admin page confirmed', ticketed, admin)
    assert tournament_match_service.admin_set_and_confirm_match(
        semi.id, admin.id, scores, confirmation_comment='Passt so'
    ).is_ok()

    _, full = clients['full']
    html = _match_page(full, semi.id)
    _, body = _comment_form(html)
    assert 'formaction=' not in body
    assert _forms_posting_to(html, 'confirm_with_scores') == []
    assert _score_inputs(html) == []
    assert any(marker in html for marker in MARKER_TEXTS)

    _, comment_only = clients['comment_only']
    _, body = _comment_form(_match_page(comment_only, semi.id))
    assert 'formaction=' not in body

    _, confirm_only = clients['confirm_only']
    html = _match_page(confirm_only, semi.id)
    assert COMMENT_FORM_ID not in html
    assert any(marker in html for marker in MARKER_TEXTS)


def test_admin_match_page_buttons_follow_the_held_permissions(
    party, ticketed, admin, clients
):
    _, semi, scores = _se_match('Admin page permissions', ticketed, admin)

    _, comment_only = clients['comment_only']
    html = _match_page(comment_only, semi.id)
    _, body = _comment_form(html)
    assert 'formaction=' not in body
    assert 'formnovalidate' in body
    assert _score_inputs(html) == []

    _, confirm_only = clients['confirm_only']
    html = _match_page(confirm_only, semi.id)
    _, body = _comment_form(html)
    assert 'formaction=' in body
    assert 'confirm_with_scores' in body
    assert 'formnovalidate' not in body
    assert len(_score_inputs(html)) == len(scores)


def test_admin_ffa_match_page_confirm_requires_comment(
    party, ticketed, admin, clients
):
    _, match, placements = _ffa_match('Admin page FFA', ticketed, admin)
    assert tournament_match_service.set_ffa_placements(
        match.id, placements
    ).is_ok()
    user, client = clients['full']
    url = f'{ADMIN_URL}/matches/{match.id}/confirm_ffa'

    html = _match_page(client, match.id)
    (opening,) = _forms_with_formaction(html, 'confirm_ffa')
    assert f"id='{COMMENT_FORM_ID}'" in opening
    assert _forms_posting_to(html, 'confirm_ffa') == []

    for blank in ({}, {'comment': ' '}):
        response = client.post(url, data=blank)

        assert response.status_code == 302
        flashes = _flashes(client)
        assert any(text in flashes for text in BLANK_TEXTS), flashes
        assert not _confirmed_now(match.id)
        assert _committed_comments(match.id) == []

    response = client.post(url, data={'comment': 'Platzierungen stimmen'})

    assert response.status_code == 302
    assert _confirmed_now(match.id)
    (stored,) = _committed_comments(match.id)
    assert stored['comment'] == 'Platzierungen stimmen'
    assert stored['context'] == 'orga_confirmation'
    assert stored['created_by'] == user.id
    html = _match_page(client, match.id)
    assert _forms_with_formaction(html, 'confirm_ffa') == []
    assert any(marker in html for marker in MARKER_TEXTS)

    _, comment_only = clients['comment_only']
    _, body = _comment_form(_match_page(comment_only, match.id))
    assert 'formaction=' not in body
