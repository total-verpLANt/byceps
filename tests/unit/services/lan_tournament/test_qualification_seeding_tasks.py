from pathlib import Path
from types import SimpleNamespace

from babel.messages.extract import extract_from_file
from babel.messages.pofile import read_po
import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
    tournament_qualification_domain_service as domain,
    tournament_qualification_service as service,
)
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)


_ROOT = Path(__file__).resolve().parents[4]
_PO = _ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'
_VIEW_HELPERS = (
    _ROOT / 'byceps/services/lan_tournament/lan_tournament_view_helpers.py'
)

NAMES = {'a': 'Kaffeesatz', 'b': 'zer0day', 'c': 'Quersumme', 'd': 'Nachtfalke'}

NEW_MSGIDS = {
    'seeding_heading': 'Seeding needs attention',
    'seeding_text': (
        'All qualifiers are known, but the playoff seeding contains equal'
        ' seeds.'
    ),
    'seeding_hint': 'Order the equal seeds to open the release.',
}


@pytest.fixture
def strings(app):
    with app.app_context(), app.test_request_context('/'):
        yield helpers.qualification_strings()


def _seed_band():
    return domain.TieBlock(
        scope=domain.CROSSOVER_SCOPE,
        contestant_ids=('b', 'c'),
        rank_from=2,
        rank_to=3,
        decided=False,
        kind=domain.TieKind.SEEDING,
    )


def _state(*, blockers=(), seeding_ties=(), ready=True, source='groups'):
    return service.QualificationState(
        tournament_id='t0',
        source=source,
        rankings=(),
        blockers=blockers,
        open_match_count=0,
        total_match_count=6,
        ready=ready,
        qualifiers=None,
        released_at=None,
        released_by=None,
        release_mode=PlayoffReleaseMode.MANUAL,
        auto_release_suspended=False,
        can_unrelease=False,
        seeding_ties=seeding_ties,
    )


def _tournament(status=TournamentStatus.ONGOING):
    return SimpleNamespace(
        tournament_status=status,
        playoff_qualifiers_per_group=2,
        playoff_qualifier_count=4,
        leaderboard_closed_at=None,
    )


def _serialize(state, strings):
    return helpers.serialize_qualification(
        state, NAMES, strings, tournament=_tournament()
    )


def test_serialize_qualification_reports_seeding_ties_apart_from_blockers(
    strings,
):
    payload = _serialize(_state(seeding_ties=(_seed_band(),)), strings)

    assert payload['ready'] is True
    assert payload['blockers'] == []
    assert payload['seeding_open'] is True
    (band,) = payload['seeding_ties']
    assert band['scope'] == domain.CROSSOVER_SCOPE
    assert band['scope_label'] == strings['scope_crossover']
    assert band['kind'] == 'seeding'
    assert band['decided'] is False
    assert band['places'] == '2–3'
    assert band['rank_from'] == 2
    assert band['text'] == strings['tie_seeding']
    assert [c['name'] for c in band['contestants']] == ['zer0day', 'Quersumme']


def test_serialize_qualification_keeps_a_cut_tie_in_the_blockers(strings):
    cut = domain.TieBlock(
        scope='group:0',
        contestant_ids=('a', 'b'),
        rank_from=2,
        rank_to=3,
        decided=False,
        kind=domain.TieKind.CUT,
    )

    payload = _serialize(
        _state(blockers=(cut,), seeding_ties=(_seed_band(),), ready=False),
        strings,
    )

    assert [b['scope'] for b in payload['blockers']] == ['group:0']
    assert [t['scope'] for t in payload['seeding_ties']] == [
        domain.CROSSOVER_SCOPE
    ]


def test_serialize_qualification_always_carries_the_seeding_keys(strings):
    payload = _serialize(_state(), strings)

    assert payload['seeding_ties'] == []
    assert payload['seeding_open'] is False


def test_playoff_release_open_is_false_while_seeding_ties_remain():
    tournament = _tournament()

    assert helpers.playoff_release_open(tournament, _state()) is True
    assert not helpers.playoff_release_open(
        tournament, _state(seeding_ties=(_seed_band(),))
    )


def test_playoff_waiting_reason_is_release_while_only_a_seed_band_is_open():
    state = _state(seeding_ties=(_seed_band(),))

    reason = helpers.playoff_waiting_reason(state, _tournament())

    assert reason == 'release'


def test_playoff_waiting_reason_still_reports_a_cut_tie():
    cut = domain.TieBlock(
        scope='group:0',
        contestant_ids=('a', 'b'),
        rank_from=2,
        rank_to=3,
        decided=False,
        kind=domain.TieKind.CUT,
    )

    reason = helpers.playoff_waiting_reason(
        _state(blockers=(cut,), seeding_ties=(_seed_band(),), ready=False),
        _tournament(),
    )

    assert reason == 'tie'


@pytest.fixture(scope='module')
def catalogue():
    with _PO.open('rb') as file:
        return read_po(file, locale='de')


@pytest.fixture(scope='module')
def view_helper_msgids():
    return {
        message[0] if isinstance(message, tuple) else message
        for _, message, _, _ in extract_from_file(
            'python', _VIEW_HELPERS, keywords={'gettext': None}
        )
    }


@pytest.mark.parametrize('key', list(NEW_MSGIDS))
def test_new_seeding_strings_have_german_translations(
    key, strings, catalogue, view_helper_msgids
):
    msgid = NEW_MSGIDS[key]

    assert strings[key] == msgid
    assert msgid in view_helper_msgids
    message = catalogue.get(msgid)
    assert message is not None, f'no catalogue entry for {msgid!r}'
    assert not message.fuzzy
    assert message.string
    assert message.string != msgid
