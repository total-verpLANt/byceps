from flask_babel import force_locale
import pytest

from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    format_file_size,
)


@pytest.fixture
def ctx(app):
    with app.app_context(), app.test_request_context('/'):
        yield


# fmt: off
@pytest.mark.parametrize(
    ('locale', 'byte_size', 'expected'),
    [
        ('de', 0,         '0 KB'),
        ('en', 0,         '0 KB'),
        ('en', -5,        '0 KB'),
        ('de', 4518,      '4 KB'),
        ('de', 634880,    '620 KB'),
        ('en', 4518,      '4 KB'),
        ('de', 1048575,   '1.024 KB'),
        ('en', 1048575,   '1,024 KB'),
    ],
)
# fmt: on
def test_format_file_size_uses_kb_below_one_mebibyte(
    ctx, locale, byte_size, expected
):
    with force_locale(locale):
        assert format_file_size(byte_size) == expected


# fmt: off
@pytest.mark.parametrize(
    ('locale', 'expected'),
    [
        ('de', '2,3 MB'),
        ('en', '2.3 MB'),
    ],
)
# fmt: on
def test_format_file_size_uses_mb_with_one_decimal(ctx, locale, expected):
    with force_locale(locale):
        assert format_file_size(2411724) == expected


@pytest.mark.parametrize('byte_size', [1, 100, 511])
def test_format_file_size_never_shows_zero_kb(ctx, byte_size):
    with force_locale('en'):
        assert format_file_size(byte_size) == '1 KB'
