"""
tests.unit.services.lan_tournament.test_create_wizard_views_admin
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from contextlib import ExitStack
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4, uuid5

from flask import Flask, g, request
from flask_babel import Babel
import pytest
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import RequestEntityTooLarge

from byceps.services.lan_tournament import tournament_image_service
from byceps.services.lan_tournament.blueprints.admin import views
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequestStatus,
)
from byceps.services.lan_tournament.models.validation_message import (
    ValidationMessage,
)
from byceps.util.result import Err, Ok


_V = 'byceps.services.lan_tournament.blueprints.admin.views'

_ALL_PERMISSIONS = frozenset(
    {
        'lan_tournament.create',
        'lan_tournament.request_view',
        'lan_tournament.request_decide',
    }
)

_REQUEST_ID = '11111111-1111-1111-1111-111111111111'

_VALID_DATA = {
    'name': 'New Tournament',
    'contestant_type': 'SOLO',
    'game_format': 'ONE_V_ONE',
    'elimination_mode': 'SINGLE_ELIMINATION',
}


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'
    Babel(a)
    return a


def _make_user(permissions=_ALL_PERMISSIONS) -> MagicMock:
    user = MagicMock()
    user.has_permission.side_effect = lambda permission: (
        permission in permissions
    )
    return user


def _make_request(status=TournamentRequestStatus.accepted):
    return SimpleNamespace(
        id=_REQUEST_ID,
        party_id='p1',
        number=7,
        proposer_id='u1',
        status=status,
        tournament_deleted=False,
    )


def _make_tournament(*, party_id='p1'):
    return SimpleNamespace(
        id='22222222-2222-2222-2222-222222222222',
        name='New Tournament',
        party_id=party_id,
    )


def _post(
    app,
    data,
    *,
    view='create',
    permissions=_ALL_PERMISSIONS,
    tournament_request=None,
    create_result=None,
    existing_by_token=None,
    attachable_image=None,
    store_result=None,
    stub_gettext=True,
):
    """Drive a create view with collaborators mocked; return the mocks."""
    mocks = SimpleNamespace()
    with ExitStack() as stack:

        def enter(target, **kwargs):
            return stack.enter_context(patch(f'{_V}.{target}', **kwargs))

        mocks.repo = enter('tournament_request_repository')
        mocks.party_svc = enter('party_service')
        mocks.tournament_svc = enter('tournament_service')
        mocks.request_svc = enter('tournament_request_service')
        mocks.create_form = enter('create_form')
        mocks.build_refusal = enter(
            '_build_refusal', return_value={'lead': 'stub'}
        )
        mocks.flash_error = enter('flash_error')
        mocks.flash_notice = enter('flash_notice')
        mocks.flash_success = enter('flash_success')
        mocks.redirect_to = enter('redirect_to')
        mocks.clear_link = enter(
            '_clear_stale_request_link', wraps=views._clear_stale_request_link
        )
        mocks.first_error_step = enter(
            'first_error_step', wraps=views.first_error_step
        )
        if stub_gettext:
            enter('gettext', side_effect=lambda msg, **kw: msg)
        mocks.find_image = stack.enter_context(
            patch(
                'byceps.services.lan_tournament.tournament_image_service'
                '.find_attachable_image',
                return_value=attachable_image,
            )
        )
        mocks.store_image = stack.enter_context(
            patch(
                'byceps.services.lan_tournament.tournament_image_service'
                '.store_uploaded_image',
                return_value=store_result,
            )
        )
        stack.enter_context(
            app.test_request_context('/', method='POST', data=data)
        )

        mocks.repo.find_request.return_value = tournament_request
        mocks.party_svc.find_party.return_value = SimpleNamespace(
            id='p1', brand_id='b1'
        )
        lookup = mocks.tournament_svc.find_tournament_by_creation_token
        if isinstance(existing_by_token, list):
            lookup.side_effect = existing_by_token
        else:
            lookup.return_value = existing_by_token
        mocks.tournament_svc.DUPLICATE_SUBMISSION_ERROR = (
            'This tournament has already been created.'
        )
        mocks.tournament_svc.IMAGE_UNAVAILABLE_ERROR = (
            tournament_image_service.IMAGE_UNAVAILABLE_ERROR
        )
        mocks.tournament_svc.create_tournament.return_value = (
            create_result
            if create_result is not None
            else Ok((_make_tournament(), MagicMock()))
        )
        mocks.request_svc.appoint_proposer_orga.return_value = Ok(None)
        mocks.create_form.return_value = 'rendered-form'
        g.user = _make_user(permissions)

        mocks.result = getattr(views, view)('p1')
    mocks.form = (
        mocks.create_form.call_args.args[1]
        if mocks.create_form.called
        else None
    )
    return mocks


# --------------------------------------------------------------------- #
# create_form
# --------------------------------------------------------------------- #


def _get_create_form(app, *, erroneous_form=None):
    with (
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}._create_wizard_urls', return_value={'create': '/c'}),
        patch(
            f'{_V}.build_create_wizard_context', return_value={'stub': True}
        ) as mock_build,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render,
        app.test_request_context('/'),
    ):
        mock_party_svc.find_party.return_value = SimpleNamespace(
            id='p1', brand_id='b1'
        )
        mock_render.return_value = 'rendered'
        g.user = _make_user()

        views.create_form('p1', erroneous_form)

    return mock_render.call_args.kwargs, mock_build


def test_create_form_sets_submission_token_once(app):
    context, _ = _get_create_form(app)
    token = context['form'].submission_token.data

    assert str(UUID(token)) == token

    # A re-render keeps the token of the failed POST.
    with app.test_request_context('/', method='POST', data={}):
        form = views._build_create_form(MultiDict({'submission_token': token}))
    context, _ = _get_create_form(app, erroneous_form=form)

    assert context['form'].submission_token.data == token


def test_create_form_keeps_f17_context_keys(app):
    context, mock_build = _get_create_form(app)

    assert {
        'party',
        'form',
        'source_request',
        'source_proposer_name',
        'source_request_blocking_field_labels',
        'wizard',
    } <= set(context)
    assert context['wizard'] == {'stub': True}
    kwargs = mock_build.call_args.kwargs
    assert kwargs['source_request'] is None
    assert kwargs['staged_image'] is None
    assert kwargs['urls'] == {'create': '/c'}


# --------------------------------------------------------------------- #
# create: field-level errors
# --------------------------------------------------------------------- #


def test_invalid_combination_is_field_error_not_flash(app):
    data = {**_VALID_DATA, 'elimination_mode': 'NONE'}

    mocks = _post(app, data)

    mocks.flash_error.assert_not_called()
    mocks.tournament_svc.create_tournament.assert_not_called()
    assert mocks.form.elimination_mode.errors == [
        'This combination of game format and elimination mode is not supported.'
    ]


def test_blank_contestant_type_is_field_error(app):
    data = {k: v for k, v in _VALID_DATA.items() if k != 'contestant_type'}

    mocks = _post(app, data)

    mocks.tournament_svc.create_tournament.assert_not_called()
    assert mocks.form.contestant_type.errors == [
        'Please choose whether individuals or teams compete.'
    ]


def test_highscore_forces_elimination_mode_none(app):
    data = {
        **_VALID_DATA,
        'game_format': 'HIGHSCORE',
        'elimination_mode': 'SINGLE_ELIMINATION',
        'score_ordering': 'HIGHER_IS_BETTER',
    }

    mocks = _post(app, data)

    mocks.create_form.assert_not_called()
    kwargs = mocks.tournament_svc.create_tournament.call_args.kwargs
    assert kwargs['elimination_mode'] is EliminationMode.NONE


def test_settings_errors_land_on_fields(app):
    data = {**_VALID_DATA, 'game_format': 'FREE_FOR_ALL'}

    mocks = _post(app, data)

    mocks.tournament_svc.create_tournament.assert_not_called()
    mocks.flash_error.assert_not_called()
    form = mocks.form
    assert form.point_table.errors == ['Add points for at least place 1.']
    assert form.group_size_max.errors == ['Required for Free-for-All.']
    assert list(form.form_errors) == []


def test_settings_error_label_param_is_translated_before_interpolation(app):
    message = ValidationMessage(
        'Must be at least "%(other)s" (%(n)s).',
        (('other', 'Min. players'), ('n', 10)),
    )

    with app.test_request_context('/', method='POST'):
        form = views._build_create_form(MultiDict())
        with patch(
            f'{_V}.gettext', side_effect=lambda msg, **kw: msg % kw
        ) as mock_gettext:
            views._apply_settings_errors(form, {'max_players': message})

    assert mock_gettext.call_args_list[0].args == ('Min. players',)
    assert form.max_players.errors == ['Must be at least "Min. players" (10).']


def test_settings_error_on_unknown_field_becomes_form_error(app):
    message = ValidationMessage('Something is off.')

    with app.test_request_context('/', method='POST'):
        form = views._build_create_form(MultiDict())
        views._apply_settings_errors(form, {'no_such_field': message})

    assert list(form.form_errors) == ['Something is off.']


# --------------------------------------------------------------------- #
# create: idempotency token
# --------------------------------------------------------------------- #


def test_existing_token_same_party_redirects_to_existing(app):
    existing = _make_tournament(party_id='p1')
    token = str(uuid4())

    mocks = _post(
        app,
        {**_VALID_DATA, 'submission_token': token},
        existing_by_token=existing,
    )

    mocks.tournament_svc.create_tournament.assert_not_called()
    mocks.flash_notice.assert_called_once_with(
        'This tournament has already been created.'
    )
    mocks.redirect_to.assert_called_once_with(
        '.view', tournament_id=existing.id
    )


def test_existing_token_other_party_is_ignored(app):
    token = str(uuid4())

    mocks = _post(
        app,
        {**_VALID_DATA, 'submission_token': token},
        existing_by_token=_make_tournament(party_id='other-party'),
    )

    kwargs = mocks.tournament_svc.create_tournament.call_args.kwargs
    assert kwargs['creation_token'] == uuid5(UUID(token), 'p1')
    mocks.redirect_to.assert_called_once_with(
        '.view', tournament_id=_make_tournament().id
    )


def test_duplicate_submission_race_redirects_to_existing(app):
    existing = _make_tournament(party_id='p1')

    mocks = _post(
        app,
        {**_VALID_DATA, 'submission_token': str(uuid4())},
        # The pre-check finds nothing; the race makes it appear later.
        existing_by_token=[None, existing],
        create_result=Err('This tournament has already been created.'),
    )

    mocks.create_form.assert_not_called()
    mocks.redirect_to.assert_called_once_with(
        '.view', tournament_id=existing.id
    )


def test_any_create_err_redirects_to_same_party_token_tournament(app):
    existing = _make_tournament(party_id='p1')

    mocks = _post(
        app,
        {
            **_VALID_DATA,
            'submission_token': str(uuid4()),
            'from_request_id': _REQUEST_ID,
        },
        tournament_request=_make_request(),
        existing_by_token=[None, existing],
        create_result=Err(
            'A tournament has already been created from this request.'
        ),
    )

    mocks.create_form.assert_not_called()
    mocks.clear_link.assert_not_called()
    mocks.request_svc.appoint_proposer_orga.assert_not_called()
    mocks.redirect_to.assert_called_once_with(
        '.view', tournament_id=existing.id
    )


# --------------------------------------------------------------------- #
# create: request refusals become `from_request_id` errors
# --------------------------------------------------------------------- #


def test_stale_request_is_from_request_id_field_error(app):
    mocks = _post(
        app,
        {**_VALID_DATA, 'from_request_id': _REQUEST_ID},
        tournament_request=_make_request(TournamentRequestStatus.rejected),
    )

    mocks.tournament_svc.create_tournament.assert_not_called()
    mocks.flash_error.assert_not_called()
    assert len(mocks.form.from_request_id.errors) == 1
    assert 'is no longer accepted' in mocks.form.from_request_id.errors[0]
    mocks.clear_link.assert_called_once()
    assert mocks.form.from_request_id.data == ''
    refused = mocks.create_form.call_args.kwargs['refused_request']
    assert refused.id == _REQUEST_ID


def test_refused_request_reaches_the_form_although_the_link_is_cleared(app):
    mocks = _post(
        app,
        {**_VALID_DATA, 'from_request_id': _REQUEST_ID},
        tournament_request=_make_request(TournamentRequestStatus.withdrawn),
    )

    assert mocks.form.from_request_id.data == ''
    refused = mocks.create_form.call_args.kwargs['refused_request']
    assert refused.status is TournamentRequestStatus.withdrawn


def test_request_permission_refusal_is_from_request_id_error(app):
    mocks = _post(
        app,
        {**_VALID_DATA, 'from_request_id': _REQUEST_ID},
        permissions=frozenset({'lan_tournament.create'}),
        tournament_request=_make_request(),
    )

    mocks.repo.find_request.assert_not_called()
    mocks.tournament_svc.create_tournament.assert_not_called()
    mocks.flash_error.assert_not_called()
    assert mocks.form.from_request_id.errors == [
        'You are not allowed to create a tournament from a request.'
    ]
    mocks.clear_link.assert_called_once()
    assert mocks.create_form.call_args.kwargs['refused_request'] is None


def test_refusal_details_are_not_looked_up_without_request_rights(app):
    mocks = _post(
        app,
        {**_VALID_DATA, 'from_request_id': _REQUEST_ID},
        view='validate_create',
        permissions=frozenset({'lan_tournament.create'}),
        tournament_request=_make_request(TournamentRequestStatus.withdrawn),
    )

    assert mocks.result.get_json()['refusal'] is None
    mocks.repo.find_request.assert_not_called()
    mocks.build_refusal.assert_not_called()


# --------------------------------------------------------------------- #
# create: images
# --------------------------------------------------------------------- #


def test_unavailable_image_is_image_id_field_error(app):
    image_id = str(uuid4())

    mocks = _post(
        app,
        {**_VALID_DATA, 'image_id': image_id},
        attachable_image=None,
    )

    mocks.tournament_svc.create_tournament.assert_not_called()
    mocks.flash_error.assert_not_called()
    assert mocks.form.image_id.errors == [
        tournament_image_service.IMAGE_UNAVAILABLE_ERROR
    ]
    assert mocks.form.image_id.data == ''


def test_available_image_is_attached_by_parsed_uuid(app):
    image = SimpleNamespace(id=uuid4())

    mocks = _post(
        app,
        {**_VALID_DATA, 'image_id': str(image.id), 'image_alt_text': ' Alt '},
        attachable_image=image,
    )

    kwargs = mocks.tournament_svc.create_tournament.call_args.kwargs
    assert kwargs['image_id'] == image.id
    assert kwargs['image_alt_text'] == 'Alt'
    parsed = mocks.find_image.call_args.args[0]
    assert isinstance(parsed, UUID)


def test_image_fk_race_is_image_id_field_error(app):
    image = SimpleNamespace(id=uuid4())

    mocks = _post(
        app,
        {**_VALID_DATA, 'image_id': str(image.id)},
        attachable_image=image,
        create_result=Err(tournament_image_service.IMAGE_UNAVAILABLE_ERROR),
    )

    mocks.flash_error.assert_not_called()
    assert mocks.form.image_id.errors == [
        tournament_image_service.IMAGE_UNAVAILABLE_ERROR
    ]
    assert list(mocks.form.form_errors) == []
    assert mocks.form.image_id.data == ''


def test_nojs_upload_then_failed_create_keeps_staged_image_id(app):
    image = SimpleNamespace(id=uuid4())

    mocks = _post(
        app,
        {**_VALID_DATA, 'image': (BytesIO(b'raw'), 'cover.png')},
        store_result=Ok(image),
        create_result=Err('Tournament name must not exceed 80 characters.'),
    )

    mocks.store_image.assert_called_once()
    kwargs = mocks.tournament_svc.create_tournament.call_args.kwargs
    assert kwargs['image_id'] == image.id
    assert mocks.form.image_id.data == str(image.id)
    assert list(mocks.form.form_errors) == [
        'Tournament name must not exceed 80 characters.'
    ]


def test_nojs_upload_error_is_image_field_error(app):
    mocks = _post(
        app,
        {**_VALID_DATA, 'image': (BytesIO(b'raw'), 'cover.png')},
        store_result=Err(
            ValidationMessage(tournament_image_service.IMAGE_CORRUPT_ERROR)
        ),
    )

    mocks.tournament_svc.create_tournament.assert_not_called()
    mocks.flash_error.assert_not_called()
    assert mocks.form.image.errors == [
        tournament_image_service.IMAGE_CORRUPT_ERROR
    ]


# --------------------------------------------------------------------- #
# create: body limit
# --------------------------------------------------------------------- #


def test_create_sets_body_limit_before_touching_the_form(app):
    seen = []

    def spy():
        seen.append(request.max_content_length)
        return MultiDict()

    with patch(f'{_V}._get_create_formdata', side_effect=spy):
        _post_limit_probe(app)

    assert seen == [tournament_image_service.MAX_REQUEST_BYTES]


def _post_limit_probe(app):
    with (
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.create_form', return_value='x'),
        app.test_request_context('/', method='POST'),
    ):
        mock_party_svc.find_party.return_value = SimpleNamespace(id='p1')
        g.user = _make_user()
        views.create('p1')


def test_create_over_body_limit_rerenders_form_with_image_error(app):
    with (
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.create_form') as mock_create_form,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}._get_create_formdata', side_effect=RequestEntityTooLarge),
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context('/', method='POST'),
    ):
        mock_party_svc.find_party.return_value = SimpleNamespace(id='p1')
        mock_create_form.return_value = 'rendered-form'
        g.user = _make_user()

        response = views.create('p1')

    assert response == ('rendered-form', 413)
    mock_create_form.assert_called_once_with(
        'p1', image_error=tournament_image_service.IMAGE_SIZE_ERROR
    )
    mock_flash_error.assert_not_called()


def test_validate_create_over_body_limit_is_json_413(app):
    class Oversized:
        content_length = None

        @property
        def form(self):
            raise RequestEntityTooLarge

    with (
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.request', Oversized()),
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context('/', method='POST'),
    ):
        mock_party_svc.find_party.return_value = SimpleNamespace(id='p1')
        g.user = _make_user()

        response, status = views.validate_create('p1')

    assert status == 413
    assert response.get_json()['error'] == (
        tournament_image_service.IMAGE_SIZE_ERROR
    )


# --------------------------------------------------------------------- #
# validate_create
# --------------------------------------------------------------------- #


def test_validate_create_returns_errors_json_and_never_creates(app):
    data = {k: v for k, v in _VALID_DATA.items() if k != 'contestant_type'}

    with app.app_context():
        mocks = _post(app, data, view='validate_create')
        body = mocks.result.get_json()

    assert mocks.result.status_code == 200
    assert body['ok'] is False
    assert body['errors']['contestant_type'] == [
        'Please choose whether individuals or teams compete.'
    ]
    assert body['first_error_step'] == 1
    assert datetime.strptime(body['checked_at'], '%H:%M')
    assert mocks.tournament_svc.method_calls == []
    mocks.store_image.assert_not_called()
    mocks.create_form.assert_not_called()
    mocks.flash_error.assert_not_called()
    mocks.flash_success.assert_not_called()


def test_validate_create_ok_for_valid_data(app):
    mocks = _post(app, _VALID_DATA, view='validate_create')
    body = mocks.result.get_json()

    assert body['ok'] is True
    assert body['errors'] == {}
    assert body['first_error_step'] is None
    assert mocks.tournament_svc.method_calls == []


def test_validate_create_sets_body_limit_first(app):
    seen = []
    build_form = views._build_create_form

    def spy(formdata):
        seen.append(request.max_content_length)
        return build_form(formdata)

    with (
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}._build_create_form', side_effect=spy),
        app.test_request_context('/', method='POST', data=_VALID_DATA),
    ):
        mock_party_svc.find_party.return_value = SimpleNamespace(id='p1')
        g.user = _make_user()
        views.validate_create('p1')

    assert seen == [tournament_image_service.MAX_REQUEST_BYTES]


def test_validate_create_never_flashes_and_never_clears_the_request_link(
    app,
):
    mocks = _post(
        app,
        {**_VALID_DATA, 'from_request_id': _REQUEST_ID},
        view='validate_create',
        tournament_request=_make_request(TournamentRequestStatus.rejected),
    )
    body = mocks.result.get_json()
    form = mocks.first_error_step.call_args.args[0]

    assert body['ok'] is False
    assert 'from_request_id' in body['errors']
    assert body['refusal'] == {'lead': 'stub'}
    mocks.flash_error.assert_not_called()
    mocks.flash_notice.assert_not_called()
    mocks.flash_success.assert_not_called()
    mocks.clear_link.assert_not_called()
    assert form.from_request_id.data == _REQUEST_ID
