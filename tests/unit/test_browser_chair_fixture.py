"""
:License: Revised BSD (see `LICENSE` file for details)
"""

from unittest.mock import Mock

from flask import Flask, current_app
import pytest

from byceps.config.converter import assemble_database_uri
from byceps.config.models import DatabaseConfig, RedisConfig

from tests.browser import serve_admin_chair_fixture as fixture


@pytest.fixture
def isolated_environment(monkeypatch):
    values = {
        'POSTGRES_HOST': '127.0.0.1',
        'POSTGRES_PORT': '55432',
        'POSTGRES_USER': 'fixture-user',
        'POSTGRES_PASSWORD': 'fixture-password',
        'POSTGRES_DB': 'byceps_chair_followup_browser',
        'REDIS_HOST': '127.0.0.1',
        'REDIS_PORT': '56379',
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for key in ['SQLALCHEMY_DATABASE_URI', 'REDIS_URL']:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    ('key', 'override'),
    [
        # Same allowed database name, but a different effective server/port.
        (
            'SQLALCHEMY_DATABASE_URI',
            'postgresql+psycopg://inherited:secret@runtime:5432/byceps_chair_followup_browser',
        ),
        (
            'SQLALCHEMY_DATABASE_URI',
            'postgresql+psycopg://fixture-user:fixture-password@127.0.0.1:55432/runtime',
        ),
        ('SQLALCHEMY_DATABASE_URI', 'not-a-url'),
        ('REDIS_URL', 'redis://inherited:secret@runtime:6379/1'),
        ('REDIS_URL', 'redis://127.0.0.1:56379/0'),
    ],
    ids=[
        'database-server',
        'database-name',
        'invalid-url',
        'redis-server',
        'redis-database',
    ],
)
def test_conflicting_override_prevents_all_resource_use_and_writes(
    isolated_environment, monkeypatch, tmp_path, key, override
):
    monkeypatch.setenv(key, override)
    steps = []
    for owner, name in [
        (fixture, 'create_admin_app'),
        (fixture.db, 'init_app'),
        (fixture, 'set_up_database'),
        (fixture, 'populate_database'),
        (fixture.brand_service, 'create_brand'),
        (fixture, 'create_user'),
    ]:
        step = Mock()
        monkeypatch.setattr(owner, name, step)
        steps.append(step)

    with pytest.raises(RuntimeError, match=key) as error:
        fixture.serve(tmp_path / 'fixture.json')

    assert str(error.value) == (
        f'{key} must match the explicit isolated browser fixture target.'
    )
    for step in steps:
        step.assert_not_called()
    assert not (tmp_path / 'fixture.json').exists()


@pytest.mark.parametrize('matching_overrides', [False, True])
def test_explicit_targets_are_shared_by_app_and_bootstrap(
    isolated_environment, monkeypatch, tmp_path, matching_overrides
):
    database = DatabaseConfig(
        host='127.0.0.1',
        port=55432,
        username='fixture-user',
        password='fixture-password',
        database='byceps_chair_followup_browser',
    )
    redis = RedisConfig(url='redis://127.0.0.1:56379/1')
    uri = assemble_database_uri(database)
    if matching_overrides:
        monkeypatch.setenv('SQLALCHEMY_DATABASE_URI', uri)
        monkeypatch.setenv('REDIS_URL', redis.url)

    initialized_uris = []
    redis_targets = []
    steps = []
    monkeypatch.setattr(
        fixture.db,
        'init_app',
        lambda app: initialized_uris.append(
            app.config['SQLALCHEMY_DATABASE_URI']
        ),
    )
    monkeypatch.setattr(
        'byceps.application.Redis.from_url',
        redis_targets.append,
    )
    monkeypatch.setattr(
        'byceps.application._enable_rq_dashboard', lambda _: None
    )
    for name in ['set_up_database', 'populate_database']:
        monkeypatch.setattr(
            fixture,
            name,
            lambda step=name: steps.append(
                (step, current_app.config['SQLALCHEMY_DATABASE_URI'])
            ),
        )

    app = fixture.create_fixture_app(tmp_path, database, redis)

    assert app.config['SQLALCHEMY_DATABASE_URI'] == uri
    assert app.config['REDIS_URL'] == redis.url
    assert initialized_uris == [uri, uri]
    assert redis_targets == [redis.url]
    assert steps == [('set_up_database', uri), ('populate_database', uri)]


def test_actual_app_target_is_rechecked_before_bootstrap(
    isolated_environment, monkeypatch, tmp_path
):
    database = DatabaseConfig(
        host='127.0.0.1',
        port=55432,
        username='fixture-user',
        password='fixture-password',
        database='byceps_chair_followup_browser',
    )
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='postgresql://secret:password@runtime/runtime',
        REDIS_URL='redis://127.0.0.1:56379/1',
    )
    monkeypatch.setattr(fixture, 'create_admin_app', lambda *_: app)
    setup = Mock()
    populate = Mock()
    monkeypatch.setattr(fixture, 'set_up_database', setup)
    monkeypatch.setattr(fixture, 'populate_database', populate)

    with pytest.raises(RuntimeError, match='SQLALCHEMY_DATABASE_URI'):
        fixture.create_fixture_app(
            tmp_path, database, RedisConfig(url=app.config['REDIS_URL'])
        )

    setup.assert_not_called()
    populate.assert_not_called()
