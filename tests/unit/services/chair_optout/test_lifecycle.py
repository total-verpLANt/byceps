"""
:License: Revised BSD (see `LICENSE` file for details)
"""

import os
import subprocess
import sys

import pytest
from sqlalchemy import event

from byceps.services.chair_optout.lifecycle import (
    _reset_after_user_change,
    enable_chair_lifecycle,
)
from byceps.services.ticketing.dbmodels.ticket import DbTicket


def test_lifecycle_registration_is_idempotent():
    enable_chair_lifecycle()
    enable_chair_lifecycle()
    listeners = list(DbTicket.__mapper__.dispatch.before_update)
    count = len(listeners)
    enable_chair_lifecycle()
    assert len(list(DbTicket.__mapper__.dispatch.before_update)) == count
    assert event.contains(DbTicket, 'before_update', _reset_after_user_change)


@pytest.mark.parametrize('mode', ['cli', 'worker'])
def test_non_web_factory_registers_lifecycle_in_fresh_process(mode):
    code = """
import sys
from pathlib import Path
from sqlalchemy import event
from byceps.application import create_cli_app, create_worker_app
from byceps.config.models import DatabaseConfig, RedisConfig
from byceps.services.chair_optout.lifecycle import _reset_after_user_change
from byceps.services.ticketing.dbmodels.ticket import DbTicket
from tests.integration.conftest import build_byceps_config
config = build_byceps_config(
    Path('/tmp'),
    DatabaseConfig(host='127.0.0.1', port=1, username='unused',
                   password='unused', database='unused'),
    RedisConfig(url='redis://127.0.0.1:1/0'),
)
factory = create_cli_app if sys.argv[1] == 'cli' else create_worker_app
factory(config)
factory(config)
assert event.contains(DbTicket, 'before_update', _reset_after_user_change)
assert 'party_ticket_chair_optouts' not in DbTicket.metadata.tables
"""
    result = subprocess.run(  # noqa: S603 - executable/code/modes are test constants.
        [sys.executable, '-c', code, mode],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            **os.environ,
            'PYTHONDONTWRITEBYTECODE': '1',
            'SQLALCHEMY_DATABASE_URI': (
                'postgresql+psycopg://unused:unused@127.0.0.1:1/unused'
            ),
            'REDIS_URL': 'redis://127.0.0.1:1/0',
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
