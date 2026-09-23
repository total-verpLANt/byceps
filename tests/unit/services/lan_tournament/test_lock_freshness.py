"""
tests.unit.services.lan_tournament.test_lock_freshness
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Regression tests for workspace-ubjc: stale SQLAlchemy identity-map
reads defeating the post-lock re-read in ``_lock_reachable_matches``
and the ``ack_critical`` gate in ``classify_result_correction``.

SQLAlchemy does not refresh an already-loaded ORM instance's
attributes on a re-query unless the read carries
``populate_existing=True`` (or the instance is expired). Every test
below that exercises the real repository functions relies on that
one, real SQLAlchemy behaviour via a small identity-map-shaped fake
session -- not on mocking it away -- so a regression that drops
``populate_existing`` from the wrong place is caught by an actual
stale-vs-fresh read, not by a mock call count.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
    TournamentMatchID,
)
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


_MATCH_ATTRS = (
    'id',
    'tournament_id',
    'group_order',
    'match_order',
    'round',
    'next_match_id',
    'bracket',
    'loser_next_match_id',
    'confirmed_by',
    'created_at',
)

_REPO_DB = 'byceps.services.lan_tournament.tournament_repository.db'


def _row(**kw) -> SimpleNamespace:
    """A backing-store row -- what a committed transaction would see
    in the database. Requires ``id`` and ``tournament_id``; everything
    else defaults the way a fresh match row would."""
    row = SimpleNamespace(
        group_order=None,
        match_order=0,
        round=0,
        next_match_id=None,
        bracket=None,
        loser_next_match_id=None,
        confirmed_by=None,
        created_at=None,
    )
    for key, value in kw.items():
        setattr(row, key, value)
    return row


class _FakeResult:
    """Stand-in for the object ``Session.execute()`` returns."""

    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class _FakeIdentityMapSession:
    """Session double reproducing ONE real SQLAlchemy behaviour: a
    SELECT for a primary key already present in the identity map does
    NOT refresh that Python object's attributes unless the read
    carries ``populate_existing=True``. A row committed by a
    "concurrent transaction" (mutating ``backing_store`` directly) is
    therefore invisible to a plain re-read of an already-loaded row,
    and visible only to a ``populate_existing`` re-read -- exactly the
    mechanism workspace-ubjc is about.

    Deliberately does NOT interpret ``with_for_update()`` (no real
    locking semantics) -- these tests are about attribute freshness,
    not lock contention.
    """

    def __init__(self):
        self.backing_store: dict = {}
        self._identity_map: dict = {}
        self.lock_calls: list[list] = []
        # One-shot callback fired the first time an id-only,
        # lock-shaped SELECT executes (i.e. lock_matches_for_update),
        # simulating a concurrent transaction's commit landing right
        # after this transaction takes its locks.
        self.on_lock = None

    def add(self, row: SimpleNamespace) -> None:
        self.backing_store[row.id] = row

    def _materialize(self, row_id, *, populate_existing: bool):
        row = self.backing_store.get(row_id)
        if row is None:
            return None
        obj = self._identity_map.get(row_id)
        first_load = obj is None
        if first_load:
            obj = SimpleNamespace()
            self._identity_map[row_id] = obj
        if populate_existing or first_load:
            for attr in _MATCH_ATTRS:
                setattr(obj, attr, getattr(row, attr))
        return obj

    def get(self, model, ident, *, populate_existing: bool = False, **kw):
        return self._materialize(ident, populate_existing=populate_existing)

    def execute(self, stmt, params=None):
        if params is not None:
            # lock_tournament_for_update: raw SQL, no rows to model.
            return _FakeResult([])

        populate_existing = bool(
            stmt.get_execution_options().get('populate_existing', False)
        )
        columns = list(stmt.selected_columns)
        params = stmt.compile().params
        values = list(params.values())

        if len(columns) == 1 and columns[0].key == 'id':
            # lock_matches_for_update: select(Model.id).filter(id.in_(ids))
            ids = next(v for v in values if isinstance(v, list))
            found = sorted(i for i in ids if i in self.backing_store)
            self.lock_calls.append(found)
            if self.on_lock is not None:
                callback, self.on_lock = self.on_lock, None
                callback()
            return _FakeResult([(i,) for i in found])

        # Full-entity select: either filter_by(id=...) or
        # filter_by(tournament_id=...).
        if len(values) == 1 and isinstance(values[0], list):
            ids = [i for i in values[0] if i in self.backing_store]
        elif len(values) == 1 and values[0] in self.backing_store:
            ids = [values[0]]
        else:
            target_tournament_id = values[0]
            ids = [
                row_id
                for row_id, row in self.backing_store.items()
                if row.tournament_id == target_tournament_id
            ]
            ids.sort(
                key=lambda row_id: (
                    self.backing_store[row_id].round,
                    self.backing_store[row_id].match_order,
                )
            )

        objs = [
            self._materialize(i, populate_existing=populate_existing)
            for i in ids
        ]
        return _FakeResult(objs)


class _FakeDb:
    def __init__(self, session):
        self.session = session


# -------------------------------------------------------------------- #
# 1) the repository read functions carry (or correctly omit) the
#    populate_existing execution option
# -------------------------------------------------------------------- #


def test_get_match_for_update_statement_requests_populate_existing():
    """The FOR UPDATE read must pair with populate_existing, or the
    lock protects the row in the database while the caller still
    reads a cached, unlocked-era Python object."""
    from byceps.services.lan_tournament import tournament_repository

    captured = {}

    class _CapturingSession:
        def execute(self, stmt):
            captured['stmt'] = stmt
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result

    with patch(_REPO_DB, _FakeDb(_CapturingSession())):
        try:
            tournament_repository.get_match_for_update(
                TournamentMatchID(generate_uuid())
            )
        except ValueError:
            pass  # not-found path; only the statement shape matters here

    assert (
        captured['stmt'].get_execution_options().get('populate_existing')
        is True
    )


def test_get_matches_for_tournament_ordered_fresh_statement_requests_populate_existing():
    from byceps.services.lan_tournament import tournament_repository

    captured = {}

    class _CapturingSession:
        def execute(self, stmt):
            captured['stmt'] = stmt
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

    with patch(_REPO_DB, _FakeDb(_CapturingSession())):
        tournament_repository.get_matches_for_tournament_ordered_fresh(
            TournamentID(generate_uuid())
        )

    assert (
        captured['stmt'].get_execution_options().get('populate_existing')
        is True
    )


def test_plain_repository_reads_do_not_request_populate_existing():
    """Contrast case: the plain (non-``_fresh``) reads must NOT carry
    populate_existing -- if they did, every unrelated caller would pay
    for a refresh it never asked for and never needed."""
    from byceps.services.lan_tournament import tournament_repository

    captured = {}

    class _CapturingSession:
        def execute(self, stmt):
            captured.setdefault('stmts', []).append(stmt)
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        def get(self, model, ident, **kw):
            captured['get_kwargs'] = kw
            return None

    with patch(_REPO_DB, _FakeDb(_CapturingSession())):
        tournament_repository.get_matches_for_tournament_ordered(
            TournamentID(generate_uuid())
        )
        tournament_repository.find_match(TournamentMatchID(generate_uuid()))

    assert (
        captured['stmts'][0].get_execution_options().get('populate_existing')
        is not True
    )
    assert captured['get_kwargs'].get('populate_existing') is not True


def test_find_match_fresh_requests_populate_existing_via_session_get():
    from byceps.services.lan_tournament import tournament_repository

    calls = []

    class _CapturingSession:
        def get(self, model, ident, *, populate_existing=False, **kw):
            calls.append(populate_existing)
            return None

    with patch(_REPO_DB, _FakeDb(_CapturingSession())):
        tournament_repository.find_match_fresh(
            TournamentMatchID(generate_uuid())
        )
        tournament_repository.find_match(TournamentMatchID(generate_uuid()))

    assert calls == [True, False]


# -------------------------------------------------------------------- #
# 2) behavioural: the fake identity-map session proves a fresh read
#    observes a change a plain read misses
# -------------------------------------------------------------------- #


def test_get_match_for_update_refreshes_a_stale_cached_row():
    from byceps.services.lan_tournament import tournament_repository

    match_id = TournamentMatchID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())
    user_id = UserID(generate_uuid())

    session = _FakeIdentityMapSession()
    session.add(_row(id=match_id, tournament_id=tournament_id))

    with patch(_REPO_DB, _FakeDb(session)):
        before = tournament_repository.find_match(match_id)
        assert before.confirmed_by is None

        # A concurrent transaction commits a confirmation.
        session.backing_store[match_id].confirmed_by = user_id

        # find_match is unchanged -- still identity-map only, still
        # stale. This documents the baseline the fix has to beat, not
        # a regression: get_match_for_update is what must see it.
        still_stale = tournament_repository.find_match(match_id)
        assert still_stale.confirmed_by is None

        locked = tournament_repository.get_match_for_update(match_id)

    assert locked.confirmed_by == user_id


def test_find_match_fresh_observes_a_change_find_match_misses():
    from byceps.services.lan_tournament import tournament_repository

    match_id = TournamentMatchID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())
    user_id = UserID(generate_uuid())

    session = _FakeIdentityMapSession()
    session.add(_row(id=match_id, tournament_id=tournament_id))

    with patch(_REPO_DB, _FakeDb(session)):
        tournament_repository.find_match(match_id)
        session.backing_store[match_id].confirmed_by = user_id

        assert tournament_repository.find_match(match_id).confirmed_by is None
        fresh = tournament_repository.find_match_fresh(match_id)

    assert fresh.confirmed_by == user_id


def test_get_matches_for_tournament_ordered_fresh_observes_a_new_edge():
    from byceps.services.lan_tournament import tournament_repository

    tournament_id = TournamentID(generate_uuid())
    m1 = TournamentMatchID(generate_uuid())
    m2 = TournamentMatchID(generate_uuid())

    session = _FakeIdentityMapSession()
    session.add(_row(id=m1, tournament_id=tournament_id))

    with patch(_REPO_DB, _FakeDb(session)):
        [loaded] = tournament_repository.get_matches_for_tournament_ordered(
            tournament_id
        )
        assert loaded.next_match_id is None

        # A concurrent bracket-reset commit wires m1 -> a new m2.
        session.add(_row(id=m2, tournament_id=tournament_id, round=1))
        session.backing_store[m1].next_match_id = m2

        stale = tournament_repository.get_matches_for_tournament_ordered(
            tournament_id
        )
        stale_m1 = next(m for m in stale if m.id == m1)
        assert stale_m1.next_match_id is None

        fresh = (
            tournament_repository.get_matches_for_tournament_ordered_fresh(
                tournament_id
            )
        )
        fresh_m1 = next(m for m in fresh if m.id == m1)

    assert fresh_m1.next_match_id == m2


# -------------------------------------------------------------------- #
# 3) behavioural, at the service seam: the two call sites the P0
#    report names directly
# -------------------------------------------------------------------- #


def test_lock_reachable_matches_relocks_when_a_concurrent_edge_appears():
    """Acceptance criterion 1: a post-lock read in
    _lock_reachable_matches must observe row state committed by
    another transaction after the pre-lock read.

    Runs the REAL repository against the fake session (tournament_
    repository is not mocked here) so the round-2 refresh is the
    genuine ``_fresh`` repository call, not a mock standing in for it.
    """
    from byceps.services.lan_tournament import tournament_match_service

    tournament_id = TournamentID(generate_uuid())
    m1 = TournamentMatchID(generate_uuid())
    m2 = TournamentMatchID(generate_uuid())

    session = _FakeIdentityMapSession()
    session.add(_row(id=m1, tournament_id=tournament_id))

    def add_m2():
        session.add(_row(id=m2, tournament_id=tournament_id, round=1))
        session.backing_store[m1].next_match_id = m2

    session.on_lock = add_m2

    with patch(_REPO_DB, _FakeDb(session)):
        tournament_match_service._lock_reachable_matches(m1)

    assert session.lock_calls == [[m1], sorted([m1, m2])]


def test_classify_result_correction_observes_a_change_committed_after_an_earlier_read():
    """Acceptance criterion 2: classify_result_correction must see
    post-lock confirmation state -- the exact scenario from
    workspace-ubjc: a second admin confirms a downstream match after
    an earlier read in this transaction already cached it unconfirmed.

    Deliberately does NOT call _lock_reachable_matches first: that
    function's own round-2 refresh would also happen to leave the
    shared object fresh, which would make this test pass even with
    classify_result_correction's OWN reads left stale (it would be
    reading an object something else already refreshed a moment
    earlier). Priming the identity map with a plain, unlocked read
    instead -- standing in for an earlier query in the same request,
    such as the admin correction-preview view's own page render --
    isolates classify_result_correction's own fix.
    """
    from byceps.services.lan_tournament import (
        tournament_match_service,
        tournament_repository,
    )

    tournament_id = TournamentID(generate_uuid())
    subject_id = TournamentMatchID(generate_uuid())
    downstream_id = TournamentMatchID(generate_uuid())
    user_id = UserID(generate_uuid())

    session = _FakeIdentityMapSession()
    session.add(
        _row(
            id=subject_id,
            tournament_id=tournament_id,
            next_match_id=downstream_id,
        )
    )
    session.add(_row(id=downstream_id, tournament_id=tournament_id, round=1))

    with patch(_REPO_DB, _FakeDb(session)):
        # An earlier, unrelated read in this same session caches both
        # matches while the downstream match is still unconfirmed.
        tournament_repository.get_matches_for_tournament_ordered(
            tournament_id
        )

        # A concurrent admin confirms the downstream match.
        session.backing_store[downstream_id].confirmed_by = user_id

        result = tournament_match_service.classify_result_correction(
            subject_id
        )

    assert result.is_ok()
    case, affected = result.unwrap()
    assert case is CorrectionCase.CONFIRMED_DOWNSTREAM
    assert affected == [downstream_id]


# -------------------------------------------------------------------- #
# 4) the ID type the blueprints actually hand in
#
# TournamentMatchID is a NewType -- a no-op at runtime -- and the admin
# routes use Flask's default string converter, so every match ID that
# reached this module from a blueprint was a plain ``str``. SQLAlchemy
# coerces one on bind, so every query kept working and nothing failed
# loudly; the UUID-keyed dict and set lookups in the two functions
# below do not coerce, so they silently missed and pinned each function
# to its pre-lock snapshot. The tests above all pass real UUIDs, which
# is precisely why they never caught it.
# -------------------------------------------------------------------- #


def test_as_match_id_normalises_a_string_and_passes_a_uuid_through():
    from uuid import UUID

    from byceps.services.lan_tournament import tournament_match_service

    match_id = TournamentMatchID(generate_uuid())

    assert tournament_match_service._as_match_id(match_id) is match_id

    normalised = tournament_match_service._as_match_id(str(match_id))
    assert isinstance(normalised, UUID)
    assert normalised == match_id


def test_lock_reachable_matches_relocks_for_a_string_match_id():
    """Same scenario as the UUID test above, with the ID in the shape
    a blueprint hands in. Before the coercion, the round-2 lookup of
    the subject match in a UUID-keyed index missed, the walk fell back
    to the pre-lock snapshot, and the concurrently added edge was
    never locked."""
    from byceps.services.lan_tournament import tournament_match_service

    tournament_id = TournamentID(generate_uuid())
    m1 = TournamentMatchID(generate_uuid())
    m2 = TournamentMatchID(generate_uuid())

    session = _FakeIdentityMapSession()
    session.add(_row(id=m1, tournament_id=tournament_id))

    def add_m2():
        session.add(_row(id=m2, tournament_id=tournament_id, round=1))
        session.backing_store[m1].next_match_id = m2

    session.on_lock = add_m2

    with patch(_REPO_DB, _FakeDb(session)):
        tournament_match_service._lock_reachable_matches(str(m1))

    assert session.lock_calls == [[m1], sorted([m1, m2])]


def test_classify_result_correction_accepts_a_string_match_id():
    """A string ID must classify identically to a UUID one, including
    the ack_critical-driving confirmed-downstream detection."""
    from byceps.services.lan_tournament import (
        tournament_match_service,
        tournament_repository,
    )

    tournament_id = TournamentID(generate_uuid())
    subject_id = TournamentMatchID(generate_uuid())
    downstream_id = TournamentMatchID(generate_uuid())
    user_id = UserID(generate_uuid())

    session = _FakeIdentityMapSession()
    session.add(
        _row(
            id=subject_id,
            tournament_id=tournament_id,
            next_match_id=downstream_id,
        )
    )
    session.add(_row(id=downstream_id, tournament_id=tournament_id, round=1))

    with patch(_REPO_DB, _FakeDb(session)):
        tournament_repository.get_matches_for_tournament_ordered(
            tournament_id
        )
        session.backing_store[downstream_id].confirmed_by = user_id

        result = tournament_match_service.classify_result_correction(
            str(subject_id)
        )

    assert result.is_ok()
    case, affected = result.unwrap()
    assert case is CorrectionCase.CONFIRMED_DOWNSTREAM
    assert affected == [downstream_id]
