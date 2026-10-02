"""
tests.unit.services.lan_tournament.test_lock_freshness
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Post-lock re-reads must observe rows committed since an earlier read
in the same session. A fake identity-map session models SQLAlchemy
not refreshing loaded instances without `populate_existing`.
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
    'phase',
    'seeding_target',
)

_REPO_DB = 'byceps.services.lan_tournament.tournament_repository.db'


def _row(**kw) -> SimpleNamespace:
    """Return a committed backing-store row."""
    row = SimpleNamespace(
        group_order=None,
        match_order=0,
        round=0,
        next_match_id=None,
        bracket=None,
        loser_next_match_id=None,
        confirmed_by=None,
        created_at=None,
        phase=1,
        seeding_target=None,
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
    """Session double honouring `populate_existing` on re-reads."""

    def __init__(self):
        self.backing_store: dict = {}
        self._identity_map: dict = {}
        self.lock_calls: list[list] = []
        # Fired once, after the first lock-shaped SELECT, to simulate a
        # concurrent commit.
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
# 1) which repository reads carry `populate_existing`
# -------------------------------------------------------------------- #


def test_get_match_for_update_statement_requests_populate_existing():
    """The `FOR UPDATE` read carries `populate_existing`."""
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
    """The plain reads do not carry `populate_existing`."""
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
# 2) a fresh read observes a change a plain read misses
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

        # `find_match` stays stale; `get_match_for_update` must see it.
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
# 3) the service call sites
# -------------------------------------------------------------------- #


def test_lock_reachable_matches_relocks_when_a_concurrent_edge_appears():
    """The post-lock read observes rows committed after the pre-lock read."""
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
    """Classify on post-lock confirmation state.

    Prime the identity map with a plain read instead of locking first,
    so only the classification's own reads are tested.
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
        # An earlier read caches both matches while still unconfirmed.
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
# 4) a `str` match ID, as a blueprint hands it in
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
    """Lock the concurrently added edge for a `str` match ID too."""
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
    """Classify a `str` ID exactly like a `UUID`."""
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
