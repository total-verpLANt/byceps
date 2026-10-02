"""
tests.unit.services.lan_tournament.test_qualification_copy
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The tie texts of the qualification payload.
"""

from pathlib import Path
from types import SimpleNamespace

from babel.messages.pofile import read_po
import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
    tournament_qualification_domain_service as domain,
    tournament_qualification_service as service,
)
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode


_PO = Path(__file__).resolve().parents[4] / (
    'byceps/translations/de/LC_MESSAGES/messages.po'
)
NAMES = {'a': 'Kaffeesatz', 'b': 'zer0day', 'c': 'Quersumme', 'd': 'Nachtfalke'}


@pytest.fixture
def strings(app):
    with app.app_context(), app.test_request_context('/'):
        yield helpers.qualification_strings()


def _tie(scope, ids, kind, *, rank_from):
    return domain.TieBlock(
        scope=scope,
        contestant_ids=ids,
        rank_from=rank_from,
        rank_to=rank_from + len(ids) - 1,
        decided=False,
        kind=kind,
    )


def _payload(ties, strings):
    entries = tuple(
        domain.RankedEntry(
            contestant_id=cid,
            rank=i + 1,
            shared=False,
            decided_by=None,
            row=None,
            value=None,
        )
        for i, cid in enumerate(NAMES)
    )
    ranking = domain.Ranking(
        scope=ties[0].scope, entries=entries, ties=tuple(ties), open_matches=0
    )
    state = service.QualificationState(
        tournament_id='t0',
        source='groups',
        rankings=(ranking,),
        blockers=tuple(ties),
        open_match_count=0,
        total_match_count=6,
        ready=False,
        qualifiers=None,
        released_at=None,
        released_by=None,
        release_mode=PlayoffReleaseMode.MANUAL,
        auto_release_suspended=False,
        can_unrelease=False,
    )
    return helpers.serialize_qualification(
        state,
        NAMES,
        strings,
        tournament=SimpleNamespace(
            playoff_qualifiers_per_group=1,
            playoff_qualifier_count=2,
            leaderboard_closed_at=None,
        ),
    )


# fmt: off
@pytest.mark.parametrize(('kind', 'rank_from'), [
    (domain.TieKind.CUT,      2),
    (domain.TieKind.SEEDING,  1),
    (domain.TieKind.SEEDING,  2),
    (domain.TieKind.HARMLESS, 3),
])
# fmt: on
def test_tie_text_has_no_appended_names(strings, kind, rank_from):
    tie = _tie('group:0', ('a', 'b'), kind, rank_from=rank_from)

    blocker = _payload([tie], strings)['blockers'][0]

    assert blocker['text'].endswith('.')
    for name in NAMES.values():
        assert name not in blocker['text']


def test_group_win_tie_label(strings):
    win = _tie('group:3', ('a', 'b'), domain.TieKind.SEEDING, rank_from=1)
    seeding = _tie('group:0', ('c', 'd'), domain.TieKind.SEEDING, rank_from=2)

    win_text = _payload([win], strings)['blockers'][0]['text']
    seeding_text = _payload([seeding], strings)['blockers'][0]['text']

    assert win_text == strings['tie_group_win']
    assert win_text.startswith('Tie for the group win.')
    assert seeding_text == strings['tie_seeding']
    assert 'seeding ranks' in seeding_text


def test_group_win_tie_label_has_german():
    with _PO.open('rb') as file:
        catalogue = read_po(file, locale='de')

    german = catalogue.get(
        'Tie for the group win. It decides the playoff seeding; the orga'
        ' sets the order.'
    ).string

    assert german.startswith('Gleichstand um den Gruppensieg.')
