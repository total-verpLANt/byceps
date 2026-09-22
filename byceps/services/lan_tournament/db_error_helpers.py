"""
byceps.services.lan_tournament.db_error_helpers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from sqlalchemy.exc import IntegrityError


def extract_constraint_name(error: IntegrityError) -> str:
    """Return the name of the violated constraint, or `''` if unknown."""
    orig = getattr(error, 'orig', None)
    if orig is None:
        return ''

    # psycopg carries the name on `diag`, not on the exception itself.
    diag = getattr(orig, 'diag', None)
    if diag is not None:
        name = getattr(diag, 'constraint_name', None)
        if isinstance(name, str) and name:
            return name

    name = getattr(orig, 'constraint_name', None)
    if isinstance(name, str) and name:
        return name

    return ''
