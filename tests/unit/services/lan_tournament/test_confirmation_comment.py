"""
tests.unit.services.lan_tournament.test_confirmation_comment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The comment an orga must give to confirm a match is checked before any lock.
"""

from unittest.mock import patch

import pytest

from byceps.services.lan_tournament import (
    tournament_config_domain_service,
    tournament_match_service,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


MATCH_ID = TournamentMatchID(generate_uuid())
USER_ID = UserID(generate_uuid())

BLANK_ERROR = 'A comment is required to confirm the match.'
LENGTH_ERROR = 'Comment cannot exceed 1000 characters.'


def test_check_confirmation_comment_none_passes():
    result = tournament_match_service._check_confirmation_comment(None)

    assert result == Ok(None)


# fmt: off
@pytest.mark.parametrize(
    'comment',
    [
        '',
        '   ',
        '\n\t',
    ],
)
# fmt: on
def test_check_confirmation_comment_refuses_blank(comment):
    result = tournament_match_service._check_confirmation_comment(comment)

    assert result == Err(BLANK_ERROR)


# fmt: off
@pytest.mark.parametrize(
    'comment, expected',
    [
        pytest.param(' x ', Ok('x'), id='stripped'),
        pytest.param('x' * 1000, Ok('x' * 1000), id='at-limit'),
        pytest.param(
            '  ' + 'x' * 1000 + '  ', Ok('x' * 1000), id='padded-at-limit'
        ),
        pytest.param('x' * 1001, Err(LENGTH_ERROR), id='over-limit'),
        pytest.param(
            ' ' + 'x' * 1001, Err(LENGTH_ERROR), id='padded-over-limit'
        ),
    ],
)
# fmt: on
def test_check_confirmation_comment_strips_and_limits(comment, expected):
    result = tournament_match_service._check_confirmation_comment(comment)

    assert result == expected


def test_check_confirmation_comment_refuses_unstorable():
    result = tournament_match_service._check_confirmation_comment('a\x00b')

    assert result == Err(
        tournament_config_domain_service.UNSTORABLE_CHARACTER_ERROR
    )


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '._lock_reachable_matches'
)
def test_admin_set_and_confirm_refuses_blank_comment_before_locking(
    mock_lock, mock_repo
):
    result = tournament_match_service.admin_set_and_confirm_match(
        MATCH_ID, USER_ID, {}, confirmation_comment='   '
    )

    assert result == Err(BLANK_ERROR)
    mock_lock.assert_not_called()
    mock_repo.rollback_session.assert_not_called()
    mock_repo.create_match_comment_flush.assert_not_called()


# fmt: off
@pytest.mark.parametrize(
    'confirm',
    [
        lambda: tournament_match_service.confirm_ffa_match(
            MATCH_ID, USER_ID, confirmation_comment='  '
        ),
        lambda: tournament_match_service.set_and_confirm_ffa_match(
            MATCH_ID, {}, USER_ID, confirmation_comment='  '
        ),
    ],
    ids=['confirm_ffa_match', 'set_and_confirm_ffa_match'],
)
# fmt: on
@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_ffa_confirms_refuse_blank_comment_before_touching_the_repository(
    mock_repo, confirm
):
    result = confirm()

    assert result == Err(BLANK_ERROR)
    assert mock_repo.mock_calls == []
