"""
tests.integration.services.lan_tournament.test_migration_017_request_permissions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from pathlib import Path

from sqlalchemy import text

from byceps.database import db
from byceps.services.authz import authz_service
from byceps.services.authz.models import PermissionID, RoleID


MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[4]
    / 'byceps'
    / 'services'
    / 'lan_tournament'
    / 'migrations'
)

REQUEST_VIEW = PermissionID('lan_tournament.request_view')
REQUEST_DECIDE = PermissionID('lan_tournament.request_decide')
ADMINISTRATE = PermissionID('lan_tournament.administrate')


def _strip_transaction_control(sql: str) -> str:
    """Remove the `BEGIN;` and `COMMIT;` lines from a migration."""
    return '\n'.join(
        line
        for line in sql.splitlines()
        if line.strip().upper() not in {'BEGIN;', 'COMMIT;'}
    )


def _granted_role_ids(connection, permission_id: PermissionID) -> set[str]:
    rows = connection.execute(
        text(
            'SELECT role_id FROM authz_role_permissions '
            'WHERE permission_id = :permission_id'
        ),
        {'permission_id': permission_id},
    ).all()
    return {row[0] for row in rows}


def test_migration_017_grants_and_rollback_017_revokes(make_role):
    """017 grants both request permissions to every role holding
    `administrate` (statements 1-2 only -- statement 3, which granted
    the canonical role by name regardless of its permissions, was
    dropped), is idempotent, and rollback_017 revokes exactly those
    grants while leaving other permission grants (e.g. administrate)
    intact.
    """
    forward_sql = _strip_transaction_control(
        (
            MIGRATIONS_DIR / '017_grant_tournament_request_permissions.sql'
        ).read_text()
    )
    rollback_sql = _strip_transaction_control(
        (MIGRATIONS_DIR / 'rollback_017.sql').read_text()
    )

    # Role holding `administrate` -- should get both new permissions.
    admin_like_role = make_role()
    authz_service.assign_permission_to_role(ADMINISTRATE, admin_like_role.id)

    # Canonical role by name, WITHOUT `administrate` -- must NOT get
    # either permission now that the by-name statement is gone.
    canonical_role_id = RoleID('lan_tournament_admin')
    authz_service.create_role(
        canonical_role_id, 'LAN-Turniere verwalten'
    ).unwrap()

    # Unrelated role -- should get neither.
    unrelated_role = make_role()

    db.session.commit()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(forward_sql)

            for permission_id in (REQUEST_VIEW, REQUEST_DECIDE):
                granted = _granted_role_ids(connection, permission_id)
                assert admin_like_role.id in granted
                assert canonical_role_id not in granted
                assert unrelated_role.id not in granted

            # Idempotent: running it again changes nothing and does
            # not error (no duplicate-key violation).
            connection.exec_driver_sql(forward_sql)

            for permission_id in (REQUEST_VIEW, REQUEST_DECIDE):
                granted = _granted_role_ids(connection, permission_id)
                assert admin_like_role.id in granted
                assert canonical_role_id not in granted
                assert unrelated_role.id not in granted

            connection.exec_driver_sql(rollback_sql)

            for permission_id in (REQUEST_VIEW, REQUEST_DECIDE):
                granted = _granted_role_ids(connection, permission_id)
                assert admin_like_role.id not in granted
                assert canonical_role_id not in granted
                assert unrelated_role.id not in granted

            # The administrate grant itself is untouched by rollback.
            administrate_granted = _granted_role_ids(connection, ADMINISTRATE)
            assert admin_like_role.id in administrate_granted
        finally:
            transaction.rollback()
