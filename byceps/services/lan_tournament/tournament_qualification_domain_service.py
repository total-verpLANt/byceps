"""
byceps.services.lan_tournament.tournament_qualification_domain_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from fractions import Fraction
from itertools import combinations
from typing import Any

from byceps.util.result import Err, Ok, Result


POINTS_WIN = 3
POINTS_DRAW = 1

CROSSOVER_SCOPE = 'crossover'

DECIDED_BY_HEAD_TO_HEAD = 'head_to_head'
DECIDED_BY_DIFFERENCE = 'difference'
DECIDED_BY_SCORES_FOR = 'scores_for'
DECIDED_BY_ORGA = 'orga'


class TieKind(Enum):
    CUT = 'cut'
    SEEDING = 'seeding'
    WINNER = 'winner'
    HARMLESS = 'harmless'


@dataclass(frozen=True, kw_only=True)
class MatchResult:
    a: str
    b: str
    score_a: int
    score_b: int
    confirmed: bool


@dataclass(frozen=True, kw_only=True)
class ResultRow:
    contestant_id: str
    played: int
    won: int
    drawn: int
    lost: int
    points: int
    score_for: int
    score_against: int

    @property
    def diff(self) -> int:
        return self.score_for - self.score_against


@dataclass(frozen=True, kw_only=True)
class RankedEntry:
    contestant_id: str
    rank: int
    shared: bool
    decided_by: str | None
    row: ResultRow | None
    value: int | None


@dataclass(frozen=True, kw_only=True)
class TieBlock:
    scope: str
    contestant_ids: tuple[str, ...]
    rank_from: int
    rank_to: int
    decided: bool
    kind: TieKind


@dataclass(frozen=True, kw_only=True)
class Ranking:
    scope: str
    entries: tuple[RankedEntry, ...]
    ties: tuple[TieBlock, ...]
    open_matches: int
    outdated: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, kw_only=True)
class Qualifier:
    contestant_id: str
    scope: str
    rank: int
    row: ResultRow | None


# -------------------------------------------------------------------- #
# rankings

_Block = tuple[tuple[str, ...], str | None]

DecisionOrders = Mapping[str, Sequence[Sequence[str]]]


def _result_rows(
    contestant_ids: Sequence[str],
    results: Sequence[MatchResult],
    walkovers: Sequence[str] = (),
) -> dict[str, ResultRow]:
    known = set(contestant_ids)
    acc = {
        cid: {'won': 0, 'drawn': 0, 'lost': 0, 'for': 0, 'against': 0}
        for cid in contestant_ids
    }
    for result in results:
        if not result.confirmed or result.a == result.b:
            continue
        if result.a not in known or result.b not in known:
            continue
        for me, mine, theirs in (
            (result.a, result.score_a, result.score_b),
            (result.b, result.score_b, result.score_a),
        ):
            row = acc[me]
            row['for'] += mine
            row['against'] += theirs
            if mine > theirs:
                row['won'] += 1
            elif mine == theirs:
                row['drawn'] += 1
            else:
                row['lost'] += 1

    for cid in walkovers:
        if cid in known:
            acc[cid]['won'] += 1

    return {
        cid: ResultRow(
            contestant_id=cid,
            played=a['won'] + a['drawn'] + a['lost'],
            won=a['won'],
            drawn=a['drawn'],
            lost=a['lost'],
            points=a['won'] * POINTS_WIN + a['drawn'] * POINTS_DRAW,
            score_for=a['for'],
            score_against=a['against'],
        )
        for cid, a in acc.items()
    }


def _split_desc(
    ids: Sequence[str], key: Callable[[str], Any]
) -> list[tuple[str, ...]]:
    """Split `ids` into groups of equal key, highest key first."""
    groups: dict[Any, list[str]] = {}
    for cid in ids:
        groups.setdefault(key(cid), []).append(cid)
    return [tuple(groups[k]) for k in sorted(groups, reverse=True)]


def _head_to_head_parts(
    ids: tuple[str, ...], results: Sequence[MatchResult]
) -> list[tuple[str, ...]]:
    """Split a tied set by its mini-table, or return it whole.

    Only valid when every pair in the set met equally often.
    """
    members = set(ids)
    meetings: Counter[frozenset[str]] = Counter()
    mini = dict.fromkeys(ids, 0)
    for result in results:
        if not result.confirmed or result.a == result.b:
            continue
        if result.a not in members or result.b not in members:
            continue
        meetings[frozenset((result.a, result.b))] += 1
        if result.score_a > result.score_b:
            mini[result.a] += POINTS_WIN
        elif result.score_a < result.score_b:
            mini[result.b] += POINTS_WIN
        else:
            mini[result.a] += POINTS_DRAW
            mini[result.b] += POINTS_DRAW

    counts = {meetings[frozenset(pair)] for pair in combinations(ids, 2)}
    if len(counts) != 1:
        return [ids]
    return _split_desc(ids, mini.__getitem__)


def _refine(
    blocks: list[_Block],
    label: str,
    splitter: Callable[[tuple[str, ...]], list[tuple[str, ...]]],
) -> list[_Block]:
    refined: list[_Block] = []
    for ids, previous in blocks:
        if len(ids) == 1:
            refined.append((ids, previous))
            continue
        parts = splitter(ids)
        if len(parts) == 1:
            refined.append((ids, previous))
            continue
        refined.extend((part, label) for part in parts)
    return refined


def _assemble(
    scope: str,
    blocks: list[_Block],
    orders: Sequence[Sequence[str]],
    make_entry: Callable[[str, int, bool, str | None], RankedEntry],
    open_matches: int,
) -> Ranking:
    ranked = {cid for ids, _ in blocks for cid in ids}
    restricted = [tuple(c for c in o if c in ranked) for o in orders]
    used: set[int] = set()
    entries: list[RankedEntry] = []
    ties: list[TieBlock] = []
    rank = 1
    for ids, label in blocks:
        if len(ids) == 1:
            entries.append(make_entry(ids[0], rank, False, label))
            rank += 1
            continue

        match = next(
            (
                i
                for i, order in enumerate(restricted)
                if i not in used
                and len(order) == len(ids)
                and set(order) == set(ids)
            ),
            None,
        )
        decided = match is not None
        if match is not None:
            used.add(match)
            ordered = list(restricted[match])
            for offset, cid in enumerate(ordered):
                entries.append(
                    make_entry(cid, rank + offset, False, DECIDED_BY_ORGA)
                )
        else:
            ordered = list(ids)
            for cid in ordered:
                entries.append(make_entry(cid, rank, True, None))
        ties.append(
            TieBlock(
                scope=scope,
                contestant_ids=tuple(ordered),
                rank_from=rank,
                rank_to=rank + len(ids) - 1,
                decided=decided,
                kind=TieKind.SEEDING,
            )
        )
        rank += len(ids)

    return Ranking(
        scope=scope,
        entries=tuple(entries),
        ties=tuple(ties),
        open_matches=open_matches,
        outdated=tuple(tuple(o) for i, o in enumerate(orders) if i not in used),
    )


def rank_round_robin(
    scope: str,
    contestant_ids: Sequence[str],
    results: Sequence[MatchResult],
    orders: Sequence[Sequence[str]] = (),
    *,
    active_ids: Collection[str] | None = None,
    walkovers: Sequence[str] = (),
) -> Ranking:
    """Rank a round robin group: points, head-to-head, difference, scores.

    A tie still standing after the chain is shared until an order in
    `orders` has exactly its members (inactive ones left out). An order
    that decides no tie is reported in `Ranking.outdated`. Tied blocks start as `TieKind.SEEDING`; call
    `classify_ties` to set the real kind.

    With `active_ids`, every other contestant is left out of the ranking,
    but their confirmed results still count for their opponents.
    """
    rows = _result_rows(contestant_ids, results, walkovers)
    known = set(contestant_ids)
    ranked_ids = (
        list(contestant_ids)
        if active_ids is None
        else [c for c in contestant_ids if c in active_ids]
    )
    open_matches = sum(
        1
        for r in results
        if not r.confirmed and r.a != r.b and r.a in known and r.b in known
    )

    blocks: list[_Block] = [
        (part, None)
        for part in _split_desc(ranked_ids, lambda c: rows[c].points)
    ]
    blocks = _refine(
        blocks,
        DECIDED_BY_HEAD_TO_HEAD,
        lambda ids: _head_to_head_parts(ids, results),
    )
    blocks = _refine(
        blocks,
        DECIDED_BY_DIFFERENCE,
        lambda ids: _split_desc(ids, lambda c: rows[c].diff),
    )
    blocks = _refine(
        blocks,
        DECIDED_BY_SCORES_FOR,
        lambda ids: _split_desc(ids, lambda c: rows[c].score_for),
    )

    def make_entry(
        cid: str, rank: int, shared: bool, decided_by: str | None
    ) -> RankedEntry:
        return RankedEntry(
            contestant_id=cid,
            rank=rank,
            shared=shared,
            decided_by=decided_by,
            row=rows[cid],
            value=None,
        )

    return _assemble(scope, blocks, orders, make_entry, open_matches)


def rank_by_value(
    scope: str,
    values: Mapping[str, int],
    *,
    higher_is_better: bool,
    orders: Sequence[Sequence[str]] = (),
) -> Ranking:
    """Rank by one value. Equal values tie; only `orders` break them."""
    sign = 1 if higher_is_better else -1
    blocks: list[_Block] = [
        (part, None)
        for part in _split_desc(list(values), lambda c: sign * values[c])
    ]

    def make_entry(
        cid: str, rank: int, shared: bool, decided_by: str | None
    ) -> RankedEntry:
        return RankedEntry(
            contestant_id=cid,
            rank=rank,
            shared=shared,
            decided_by=decided_by,
            row=None,
            value=values[cid],
        )

    return _assemble(scope, blocks, orders, make_entry, 0)


# -------------------------------------------------------------------- #
# ties


def _classify(block: TieBlock, cut: int | None, plain_winner: bool) -> TieKind:
    if cut is not None:
        if block.rank_from > cut:
            return TieKind.HARMLESS
        if block.rank_to > cut:
            return TieKind.CUT
        return TieKind.SEEDING
    if plain_winner and block.rank_from == 1:
        return TieKind.WINNER
    return TieKind.HARMLESS


def classify_ties(
    ranking: Ranking, *, cut: int | None, plain_winner: bool
) -> Ranking:
    """Set the kind of every tie block relative to the cut line.

    With a `cut`: spanning it is CUT, entirely above it is SEEDING (the
    members land in different crossover bands), below it is HARMLESS.
    Without a cut, rank 1 of a plain round robin is WINNER.
    """
    return replace(
        ranking,
        ties=tuple(
            replace(tie, kind=_classify(tie, cut, plain_winner))
            for tie in ranking.ties
        ),
    )


def blocking_ties(rankings: Sequence[Ranking]) -> tuple[TieBlock, ...]:
    """Return every undecided tie that is not harmless."""
    return tuple(
        tie
        for ranking in rankings
        for tie in ranking.ties
        if not tie.decided and tie.kind is not TieKind.HARMLESS
    )


# -------------------------------------------------------------------- #
# qualifiers


def _qualifiers_of(ranking: Ranking, cut: int) -> tuple[Qualifier, ...]:
    return tuple(
        Qualifier(
            contestant_id=e.contestant_id,
            scope=ranking.scope,
            rank=e.rank,
            row=e.row,
        )
        for e in ranking.entries
        if e.rank <= cut
    )


def qualifiers_top_per_scope(
    rankings: Sequence[Ranking], q: int
) -> Result[tuple[Qualifier, ...], tuple[TieBlock, ...]]:
    """Return the top `q` of every ranking, or the ties that block it.

    Every ranking is classified against the cut here, whatever kind its
    tie blocks carry. Open matches are not checked.
    """
    classified = [classify_ties(r, cut=q, plain_winner=False) for r in rankings]
    blockers = blocking_ties(classified)
    if blockers:
        return Err(blockers)
    return Ok(tuple(x for r in classified for x in _qualifiers_of(r, q)))


def qualifiers_top_k(
    ranking: Ranking, k: int
) -> Result[tuple[Qualifier, ...], tuple[TieBlock, ...]]:
    """Return the top `k` of one ranking, or the ties that block it."""
    return qualifiers_top_per_scope([ranking], k)


def rank_crossover(
    qualifiers: Sequence[Qualifier],
    orders: Sequence[Sequence[str]] = (),
) -> Ranking:
    """Rank qualifiers into playoff seeds, band by band.

    Winners come first, then runners-up, and so on. Within a band the
    groups are compared per match played: points, difference, scored.
    What stays equal is a seeding tie until an order in `orders` has
    exactly its members.
    """
    by_id = {q.contestant_id: q for q in qualifiers}

    def per_match(cid: str) -> tuple[Fraction, Fraction, Fraction]:
        row = by_id[cid].row
        if row is None or row.played == 0:
            return (Fraction(0), Fraction(0), Fraction(0))
        return (
            Fraction(row.points, row.played),
            Fraction(row.diff, row.played),
            Fraction(row.score_for, row.played),
        )

    blocks: list[_Block] = []
    for rank in sorted({q.rank for q in qualifiers}):
        band = [q.contestant_id for q in qualifiers if q.rank == rank]
        blocks.extend((part, None) for part in _split_desc(band, per_match))

    def make_entry(
        cid: str, rank: int, shared: bool, decided_by: str | None
    ) -> RankedEntry:
        return RankedEntry(
            contestant_id=cid,
            rank=rank,
            shared=shared,
            decided_by=decided_by,
            row=by_id[cid].row,
            value=None,
        )

    return _assemble(CROSSOVER_SCOPE, blocks, orders, make_entry, 0)


def crossover_is_exempt(qualifiers: Sequence[Qualifier]) -> bool:
    """Return `True` for exactly two qualifiers: one final, any order."""
    return len(qualifiers) == 2


def crossover_seed_list(
    qualifiers: Sequence[Qualifier],
    orders: Sequence[Sequence[str]] = (),
) -> Result[tuple[Qualifier, ...], tuple[TieBlock, ...]]:
    """Return the qualifiers in seed order, or the ties that block it."""
    ranking = rank_crossover(qualifiers, orders)
    blockers = blocking_ties([ranking])
    if blockers and not crossover_is_exempt(qualifiers):
        return Err(blockers)
    by_id = {q.contestant_id: q for q in qualifiers}
    return Ok(tuple(by_id[e.contestant_id] for e in ranking.entries))


# -------------------------------------------------------------------- #
# same-group separation


def same_group_matches(
    layout: Sequence[str | None], origin: Mapping[str, str]
) -> list[int]:
    """Return the 1-based first-round matches pairing two of one group."""
    conflicts = []
    for index in range(len(layout) // 2):
        a, b = layout[2 * index], layout[2 * index + 1]
        if a is None or b is None:
            continue
        group = origin.get(a)
        if group is not None and group == origin.get(b):
            conflicts.append(index + 1)
    return conflicts


def separate_same_group(
    layout: Sequence[str | None], origin: Mapping[str, str]
) -> tuple[tuple[str | None, ...], list[tuple[int, int]]] | None:
    """Swap lower-band slots until no first-round match pairs one group.

    Return the new layout and the 0-based slot swaps, or None when the
    greedy search finds no separation. Byes never move: only slots that
    hold a contestant are swapped.
    """
    slots = list(layout)
    swaps: list[tuple[int, int]] = []
    while conflicts := same_group_matches(slots, origin):
        match = conflicts[0] - 1
        lower = 2 * match + 1
        partner = _find_swap_partner(slots, origin, match)
        if partner is None:
            return None
        other = 2 * partner + 1
        slots[lower], slots[other] = slots[other], slots[lower]
        swaps.append((lower, other))
    return tuple(slots), swaps


def _find_swap_partner(
    slots: Sequence[str | None], origin: Mapping[str, str], match: int
) -> int | None:
    lower = slots[2 * match + 1]
    count = len(slots) // 2
    candidates = sorted(
        (m for m in range(count) if m != match),
        key=lambda m: (abs(m - match), m),
    )
    for other in candidates:
        moved = slots[2 * other + 1]
        if moved is None:
            continue
        if _pairs_one_group(slots[2 * match], moved, origin):
            continue
        if _pairs_one_group(slots[2 * other], lower, origin):
            continue
        return other
    return None


def _pairs_one_group(
    a: str | None, b: str | None, origin: Mapping[str, str]
) -> bool:
    if a is None or b is None:
        return False
    group = origin.get(a)
    return group is not None and group == origin.get(b)


# -------------------------------------------------------------------- #
# plain round robin


def plain_round_robin_winner(ranking: Ranking) -> Result[str, TieBlock]:
    """Return the rank 1 contestant, or the undecided tie for it.

    Open matches are not checked.
    """
    if not ranking.entries:
        raise ValueError('Cannot pick a winner from an empty ranking.')
    classified = classify_ties(ranking, cut=None, plain_winner=True)
    for tie in blocking_ties([classified]):
        if tie.kind is TieKind.WINNER:
            return Err(tie)
    return Ok(classified.entries[0].contestant_id)
