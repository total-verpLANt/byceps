from unittest.mock import patch
from uuid import uuid4
from pathlib import Path
import shutil
import subprocess

from flask import Flask
from flask_babel import Babel
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_service,
)
from byceps.services.lan_tournament.blueprints.admin.forms import (
    TournamentCreateForm,
    TournamentUpdateForm,
)
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    first_error_step,
    group_tournaments_by_category,
)
from byceps.services.lan_tournament.models.tournament_category import (
    TournamentCategory,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)


@pytest.fixture(scope='module')
def app():
    app = Flask(__name__)
    app.config.update(LOCALE='en', BABEL_DEFAULT_LOCALE='en')
    Babel(app)
    return app


def _tournament(**kwargs):
    return tournament_domain_service.create_tournament(
        'party', 'Cup', **kwargs
    )[0]


@pytest.mark.parametrize('category', list(TournamentCategory))
@pytest.mark.parametrize('from_request', [False, True])
def test_user_organized_describes_category_only(category, from_request):
    tournament = _tournament(
        category=category,
        created_from_request_id=uuid4() if from_request else None,
    )
    assert tournament.is_user_organized is (
        category is TournamentCategory.USER_ORGANIZED
    )


def test_grouping_uses_category_then_position_and_keeps_drafts():
    main_late = _tournament(
        category=TournamentCategory.MAIN,
        position=9,
        created_from_request_id=uuid4(),
    )
    main_early = _tournament(category=TournamentCategory.MAIN, position=2)
    fun = _tournament(category=TournamentCategory.FUN, position=0)
    draft = _tournament(
        category=TournamentCategory.STAGE,
        position=1,
        tournament_status=TournamentStatus.DRAFT,
    )
    user = _tournament(category=TournamentCategory.USER_ORGANIZED, position=0)
    tournaments = [main_late, user, fun, draft, main_early]

    grouped = group_tournaments_by_category(tournaments)

    assert list(grouped) == list(TournamentCategory)
    assert list(grouped.values()) == [
        [main_early, main_late],
        [fun],
        [draft],
        [user],
    ]
    assert tournaments == [main_late, user, fun, draft, main_early]


@pytest.mark.parametrize(
    'form_class', [TournamentCreateForm, TournamentUpdateForm]
)
@pytest.mark.parametrize('value', ['', 'UNKNOWN', None])
def test_invalid_form_category_is_a_basics_error(app, form_class, value):
    data = {'name': 'Cup'}
    if value is not None:
        data['category'] = value
    with app.test_request_context():
        form = form_class(MultiDict(data))
        form.set_contestant_type_choices()
        form.set_game_format_choices()
        form.set_elimination_mode_choices()
        form.set_score_ordering_choices()
        assert not form.validate()
        assert form.category.errors
        assert first_error_step(form) == 0


@pytest.mark.parametrize('value', ['MAIN', 'invalid', 1])
def test_invalid_service_category_is_rejected_before_repository_access(value):
    with patch.object(
        tournament_service, 'tournament_repository'
    ) as repository:
        assert tournament_service.create_tournament(
            'party', 'Cup', category=value
        ).is_err()
        assert tournament_service.update_tournament(
            uuid4(), name='Cup', category=value
        ).is_err()
        assert repository.method_calls == []


@pytest.mark.parametrize(
    'status', [TournamentStatus.ONGOING, TournamentStatus.PAUSED]
)
def test_service_rejects_category_change_during_play(status):
    tournament = _tournament(tournament_status=status)
    with patch.object(
        tournament_service, 'tournament_repository'
    ) as repository:
        repository.get_tournament.return_value = tournament
        result = tournament_service.update_tournament(
            tournament.id, name=tournament.name, category=TournamentCategory.FUN
        )
        assert result.is_err()
        assert 'category' in result.unwrap_err()
        repository.update_tournament.assert_not_called()


def test_reorder_failure_rolls_back_the_operation():
    tournament = _tournament()
    with patch.object(
        tournament_service, 'tournament_repository'
    ) as repository:
        repository.get_tournaments_for_party.return_value = [tournament]
        repository.reorder_tournaments.side_effect = RuntimeError(
            'write failed'
        )
        with pytest.raises(RuntimeError, match='write failed'):
            tournament_service.reorder_tournaments(
                tournament.party_id, [str(tournament.id)]
            )
        repository.rollback_session.assert_called_once()


@pytest.mark.skipif(
    shutil.which('node') is None, reason='Node.js is not installed'
)
def test_category_javascript_behaviour():
    root = Path(__file__).resolve().parents[4]
    result = subprocess.run(  # noqa: S603 -- fixed local test suite
        [
            shutil.which('node'),
            '--test',
            str(root / 'tests/js/lan_tournament_categories.test.js'),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
