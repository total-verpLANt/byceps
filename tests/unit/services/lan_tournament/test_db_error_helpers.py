"""
tests.unit.services.lan_tournament.test_db_error_helpers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from unittest.mock import Mock

from sqlalchemy.exc import IntegrityError

from byceps.services.lan_tournament.db_error_helpers import (
    extract_constraint_name,
)


class _PsycopgLikeDiagnostic:
    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name


class _PsycopgLikeError(Exception):
    def __init__(self, constraint_name: str) -> None:
        super().__init__('duplicate key value violates unique constraint')
        self.diag = _PsycopgLikeDiagnostic(constraint_name)


def test_reads_the_name_off_psycopg_diag():
    orig = _PsycopgLikeError('uq_lan_tournament_orgas_tournament_user')
    error = IntegrityError('', {}, orig)

    assert not hasattr(orig, 'constraint_name')
    assert (
        extract_constraint_name(error)
        == 'uq_lan_tournament_orgas_tournament_user'
    )


def test_falls_back_to_a_plain_constraint_name_attribute():
    """Other drivers expose the name on the exception itself."""

    class _PlainError(Exception):
        constraint_name = 'uq_something'

    assert extract_constraint_name(IntegrityError('', {}, _PlainError())) == (
        'uq_something'
    )


def test_ignores_a_non_string_name():
    """Ignore the `Mock` that a `Mock` auto-creates as `diag`."""
    orig = Mock(constraint_name='uq_real_name')

    assert extract_constraint_name(IntegrityError('', {}, orig)) == (
        'uq_real_name'
    )


def test_returns_empty_string_when_no_name_is_available():
    class _Bare(Exception):
        pass

    assert extract_constraint_name(IntegrityError('', {}, _Bare())) == ''
