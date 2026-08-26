"""
tests.unit.services.lan_tournament.test_admin_match_status_filter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the admin match status filter applied in
``admin/views.matches_for_tournament()``.

Every match is in exactly one readiness bucket (``waiting``, ``not_ready``,
``partially_ready``, ``both_ready``, ``no_readiness``, ``finished``).
``all`` is the unfiltered default; the legacy ``open``, ``ready`` and
``playable`` values and unknown values fall back to it.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from flask import Flask
from flask_babel import Babel
import pytest

from byceps.services.lan_tournament.blueprints.admin import views as admin
from byceps.services.lan_tournament.models.match_readiness import (
    MatchReadiness,
    READINESS_FILTER_BUCKETS,
    ReadinessDisplayStatus,
)
from byceps.services.lan_tournament.models.tournament_match import MatchSide


# -- helpers ----------------------------------------------------------------


def _projection(
    status: ReadinessDisplayStatus,
    *,
    count: int,
    ready_sides: tuple[MatchSide, ...] = (),
    outcome: str | None = None,
    supports_readiness: bool = True,
) -> MatchReadiness:
    return MatchReadiness(
        match_id=uuid4(),
        status=status,
        ready_sides=ready_sides,
        assigned_contestant_count=count,
        supports_readiness=supports_readiness,
        outcome=outcome,
    )


# One match for each way into a bucket, in list order.
# fmt: off
_PROJECTIONS = {
    'empty': (
        'waiting',
        _projection(ReadinessDisplayStatus.NOT_YET_OCCUPIED, count=0),
    ),
    'one contestant': (
        'waiting',
        _projection(ReadinessDisplayStatus.NOT_YET_OCCUPIED, count=1),
    ),
    'nobody ready': (
        'not_ready',
        _projection(ReadinessDisplayStatus.OPEN, count=2),
    ),
    'one side ready': (
        'partially_ready',
        _projection(
            ReadinessDisplayStatus.PARTIALLY_READY,
            count=2,
            ready_sides=(MatchSide.A,),
        ),
    ),
    'both sides ready': (
        'both_ready',
        _projection(
            ReadinessDisplayStatus.BOTH_READY,
            count=2,
            ready_sides=(MatchSide.A, MatchSide.B),
        ),
    ),
    'free for all': (
        'no_readiness',
        _projection(
            ReadinessDisplayStatus.OPEN, count=3, supports_readiness=False
        ),
    ),
    'confirmed': (
        'finished',
        _projection(
            ReadinessDisplayStatus.BOTH_READY,
            count=2,
            ready_sides=(MatchSide.A, MatchSide.B),
            outcome='confirmed',
        ),
    ),
    'confirmed defwin': (
        'finished',
        _projection(
            ReadinessDisplayStatus.NOT_YET_OCCUPIED,
            count=1,
            outcome='defwin',
        ),
    ),
}
# fmt: on


@pytest.fixture
def list_matches(monkeypatch):
    """Return the context of the real admin list view for a query string."""
    tournament = SimpleNamespace(
        id=uuid4(), party_id=uuid4(), has_playoffs=False
    )
    entries = [
        {
            'match': SimpleNamespace(id=projection.match_id),
            'contestants': [],
            'readiness': projection,
            'readiness_display': {},
        }
        for _, projection in _PROJECTIONS.values()
    ]
    readiness_by_match_id = {
        entry['match'].id: entry['readiness'] for entry in entries
    }
    monkeypatch.setattr(admin, '_get_tournament_or_404', lambda _: tournament)
    monkeypatch.setattr(
        admin.party_service,
        'get_party',
        lambda _: SimpleNamespace(id=tournament.party_id),
    )
    monkeypatch.setattr(
        admin,
        '_admin_match_projections',
        lambda _: (entries, readiness_by_match_id),
    )
    monkeypatch.setattr(
        admin, 'build_contestant_name_lookups', lambda *a, **kw: ({}, {})
    )
    monkeypatch.setattr(admin, 'build_hover_lookups', lambda *a, **kw: ({}, {}))
    app = Flask(__name__)
    app.config.update(TESTING=True, BABEL_DEFAULT_LOCALE='en')
    Babel(app)

    def list_matches(query: str = '') -> dict:
        with app.test_request_context('/' + query):
            return admin.matches_for_tournament.__wrapped__.__wrapped__(
                str(tournament.id)
            )

    return list_matches


def _listed_ids(context: dict) -> list:
    return [entry['match'].id for entry in context['match_data']]


def _ids_in(*buckets: str) -> list:
    return [
        projection.match_id
        for bucket, projection in _PROJECTIONS.values()
        if bucket in buckets
    ]


# -- tests ------------------------------------------------------------------


class TestAdminMatchStatusFilter:
    """Admin match status filter over the disjoint readiness buckets."""

    def test_default_filter_is_all(self, list_matches):
        """Without `only=` every match is listed and `all` is current."""
        context = list_matches()

        assert context['only'] == 'all'
        assert _listed_ids(context) == [
            projection.match_id for _, projection in _PROJECTIONS.values()
        ]

    @pytest.mark.parametrize('only', ['open', 'ready', 'playable', 'bogus', ''])
    def test_legacy_and_unknown_filters_show_all(self, list_matches, only):
        """Removed nested filters never hide a match."""
        context = list_matches(f'?only={only}')

        assert context['only'] == 'all'
        assert len(context['match_data']) == len(_PROJECTIONS)

    @pytest.mark.parametrize('bucket', READINESS_FILTER_BUCKETS)
    def test_bucket_filter_shows_only_its_matches(self, list_matches, bucket):
        """A bucket filter lists exactly the matches in that bucket."""
        context = list_matches(f'?only={bucket}')

        assert context['only'] == bucket
        assert _listed_ids(context) == _ids_in(bucket)

    def test_buckets_are_disjoint_and_cover_every_match(self, list_matches):
        """No match is in two buckets, and no match is in none."""
        listed = []
        for bucket in READINESS_FILTER_BUCKETS:
            listed += _listed_ids(list_matches(f'?only={bucket}'))

        assert sorted(listed) == sorted(_ids_in(*READINESS_FILTER_BUCKETS))
        assert len(listed) == len(set(listed)) == len(_PROJECTIONS)

    def test_confirmed_matches_leave_the_readiness_buckets(self, list_matches):
        """A confirmed result or defwin is finished, whatever the claims."""
        finished = _listed_ids(list_matches('?only=finished'))
        others = [
            match_id
            for bucket in READINESS_FILTER_BUCKETS
            if bucket != 'finished'
            for match_id in _listed_ids(list_matches(f'?only={bucket}'))
        ]

        assert finished == _ids_in('finished')
        assert not set(finished) & set(others)

    def test_hidden_bucket_request_marks_all_current(
        self, list_matches, monkeypatch
    ):
        """`no_readiness` is hidden at count 0, so it falls back to `all`."""
        kept = [
            projection
            for bucket, projection in _PROJECTIONS.values()
            if bucket != 'no_readiness'
        ]
        entries = [
            {
                'match': SimpleNamespace(id=projection.match_id),
                'contestants': [],
                'readiness': projection,
                'readiness_display': {},
            }
            for projection in kept
        ]
        monkeypatch.setattr(
            admin,
            '_admin_match_projections',
            lambda _: (
                entries,
                {entry['match'].id: entry['readiness'] for entry in entries},
            ),
        )

        context = list_matches('?only=no_readiness')

        assert context['match_quantities']['no_readiness'] == 0
        assert 'no_readiness' not in [
            key for key, _, _ in context['match_filter_options']
        ]
        assert context['only'] == 'all'
        assert _listed_ids(context) == [p.match_id for p in kept]

    def test_listed_bucket_stays_selected(self, list_matches):
        """A bucket the bar offers is not replaced by `all`."""
        context = list_matches('?only=no_readiness')

        assert 'no_readiness' in [
            key for key, _, _ in context['match_filter_options']
        ]
        assert context['only'] == 'no_readiness'
        assert _listed_ids(context) == _ids_in('no_readiness')

    def test_all_filter_shows_everything(self, list_matches):
        """The `all` filter returns every match regardless of status."""
        context = list_matches('?only=all')

        assert context['only'] == 'all'
        assert len(context['match_data']) == len(_PROJECTIONS)

    def test_counts_computed_before_filtering(self, list_matches):
        """Quantities reflect the unfiltered totals and add up to `all`."""
        expected = {
            'all': 8,
            'waiting': 2,
            'not_ready': 1,
            'partially_ready': 1,
            'both_ready': 1,
            'no_readiness': 1,
            'finished': 2,
        }

        context = list_matches('?only=both_ready')

        assert len(context['match_data']) == 1
        assert context['match_quantities'] == expected
        assert (
            sum(expected[bucket] for bucket in READINESS_FILTER_BUCKETS)
            == expected['all']
        )
