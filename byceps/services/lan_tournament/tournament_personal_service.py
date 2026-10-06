"""Personal read side. Uses saved engine results; never advances a bracket."""

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from flask_babel import lazy_gettext

from byceps.services.party.models import PartyID
from byceps.services.user import user_service
from byceps.services.user.models import User, UserID

from . import (
    tournament_orga_service,
    tournament_personal_repository as repository,
    tournament_repository,
    tournament_service,
)
from .models.bracket import Bracket
from .models.contestant_type import ContestantType
from .models.elimination_mode import EliminationMode
from .models.game_format import GameFormat, VALID_COMBINATIONS
from .models.tournament import Tournament
from .models.tournament_match import TournamentMatch, TournamentMatchID
from .models.tournament_match_to_contestant import TournamentMatchToContestant
from .models.tournament_participant import TournamentParticipant
from .models.tournament_status import TournamentStatus
from .tournament_domain_service import (
    derive_contestant_type,
    determine_match_winner,
)


class PersonalGroup(Enum):
    ONGOING = 'ongoing'
    WAITING = 'waiting'
    REGISTERED = 'registered'
    FINISHED = 'finished'

    @property
    def label(self):
        return {
            self.ONGOING: lazy_gettext('Up now'),
            self.WAITING: lazy_gettext('Waiting for a match'),
            self.REGISTERED: lazy_gettext('Signed up'),
            self.FINISHED: lazy_gettext('Completed / Eliminated'),
        }[self]


@dataclass(frozen=True, kw_only=True)
class PersonalMatch:
    match: TournamentMatch
    own_side: TournamentMatchToContestant
    opponents: list[TournamentMatchToContestant]

    @property
    def state(self) -> str:
        if self.match.confirmed_by is not None:
            return 'confirmed'
        return 'open' if self.opponents else 'waiting'


@dataclass(frozen=True, kw_only=True)
class PersonalTournament:
    tournament: Tournament
    participant: TournamentParticipant
    group: PersonalGroup
    status: str
    matches: list[PersonalMatch]
    needs_attention: bool = False
    awaiting_completion: bool = False

    @property
    def show_score_submission_prompt(self) -> bool:
        return (
            self.tournament.game_format == GameFormat.HIGHSCORE
            and self.tournament.tournament_status == TournamentStatus.ONGOING
            and self.group == PersonalGroup.ONGOING
            and not self.needs_attention
            and not self.awaiting_completion
        )

    @property
    def state(self) -> str:
        if self.needs_attention:
            return 'notice'
        return {
            PersonalGroup.ONGOING: 'open',
            PersonalGroup.WAITING: 'waiting',
            PersonalGroup.REGISTERED: 'neutral',
            PersonalGroup.FINISHED: (
                'neutral'
                if self.tournament.tournament_status
                == TournamentStatus.CANCELLED
                else 'confirmed'
            ),
        }[self.group]

    @property
    def ready_matches(self) -> list[PersonalMatch]:
        return [m for m in self.matches if m.state == 'open']

    @property
    def waiting_matches(self) -> list[PersonalMatch]:
        return [m for m in self.matches if m.state == 'waiting']

    @property
    def confirmed_matches(self) -> list[PersonalMatch]:
        return [m for m in self.matches if m.state == 'confirmed']


def evaluate_participation(
    tournament: Tournament,
    participant: TournamentParticipant,
    matches: list[TournamentMatch],
    sides: dict[TournamentMatchID, list[TournamentMatchToContestant]],
    *,
    highscore_results_complete: bool = False,
) -> PersonalTournament:
    """Evaluate current solo/team participation from a freshly loaded snapshot.

    Match order is bracket, round, group, match, ID; it is not a time forecast.
    Confirmed matches are shown only if no open own matches remain.
    """
    is_team = (
        derive_contestant_type(
            tournament.contestant_type,
            tournament.max_players_in_team,
            tournament.min_players_in_team,
        )
        == ContestantType.TEAM
    )

    def belongs(side):
        if is_team:
            return (
                participant.team_id is not None
                and side.team_id == participant.team_id
            )
        return side.participant_id == participant.id

    own = []
    if tournament.game_format != GameFormat.HIGHSCORE:
        for match in sorted(matches, key=_match_order):
            contestants = sides.get(match.id, [])
            for side in contestants:
                if belongs(side):
                    own.append(
                        PersonalMatch(
                            match=match,
                            own_side=side,
                            opponents=[
                                c
                                for c in contestants
                                if c.id != side.id
                                and (
                                    c.participant_id is not None
                                    or c.team_id is not None
                                )
                            ],
                        )
                    )

    open_matches = [m for m in own if m.match.confirmed_by is None]
    group, status, needs_attention = _classify(
        tournament, own, open_matches, matches, sides
    )
    if (
        is_team
        and participant.team_id is None
        and group != PersonalGroup.FINISHED
    ):
        status = lazy_gettext('No current team')
        needs_attention = True
        if group != PersonalGroup.REGISTERED:
            group = PersonalGroup.WAITING
    awaiting_completion = (
        highscore_results_complete
        and tournament.game_format == GameFormat.HIGHSCORE
        and tournament.tournament_status == TournamentStatus.ONGOING
        and group == PersonalGroup.ONGOING
        and not needs_attention
    )
    if awaiting_completion:
        group = PersonalGroup.WAITING
        status = lazy_gettext(
            'Waiting for the tournament orga to complete the tournament'
        )
    return PersonalTournament(
        tournament=tournament,
        participant=participant,
        group=group,
        status=status,
        matches=sorted(
            open_matches, key=lambda m: (not m.opponents, _match_order(m.match))
        )
        or own,
        needs_attention=needs_attention,
        awaiting_completion=awaiting_completion,
    )


def _match_order(match):
    brackets = {
        None: 0,
        Bracket.WINNERS: 0,
        Bracket.LOSERS: 1,
        Bracket.GRAND_FINAL: 2,
        Bracket.THIRD_PLACE: 3,
    }
    return (
        brackets[match.bracket],
        match.round or 0,
        match.group_order or 0,
        match.match_order or 0,
        str(match.id),
    )


def _classify(tournament, own, open_matches, matches, sides):
    state = tournament.tournament_status
    if state == TournamentStatus.CANCELLED:
        return PersonalGroup.FINISHED, lazy_gettext('Cancelled'), False
    if state == TournamentStatus.COMPLETED:
        # The engine completes SE on the final, even if P3 is still open.
        # Surface that inconsistent state instead of hiding the placement match.
        if any(m.match.bracket == Bracket.THIRD_PLACE for m in open_matches):
            return (
                PersonalGroup.WAITING,
                lazy_gettext(
                    'Tournament completed with an open third-place match'
                ),
                True,
            )
        if tournament.game_format == GameFormat.FREE_FOR_ALL and own:
            ffa_group, ffa_status, needs_attention = _classify_ffa(
                tournament, own, matches, sides
            )
            if needs_attention and any(
                m.bracket == Bracket.GRAND_FINAL for m in matches
            ):
                return ffa_group, ffa_status, needs_attention
        return PersonalGroup.FINISHED, lazy_gettext('Completed'), False
    if state in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
    ):
        return PersonalGroup.REGISTERED, lazy_gettext('Registered'), False
    if state not in (TournamentStatus.ONGOING, TournamentStatus.PAUSED):
        return _needs_review()

    group, status, needs_attention = _classify_running(
        tournament, own, open_matches, matches, sides
    )
    if state == TournamentStatus.PAUSED and group != PersonalGroup.FINISHED:
        group = PersonalGroup.WAITING
        status = lazy_gettext('Paused')
        needs_attention = True
    return group, status, needs_attention


def _needs_review():
    return (
        PersonalGroup.WAITING,
        lazy_gettext('Participation needs review'),
        True,
    )


def _classify_running(tournament, own, open_matches, matches, sides):
    if (
        tournament.game_format,
        tournament.elimination_mode,
    ) not in VALID_COMBINATIONS:
        return _needs_review()
    if tournament.game_format == GameFormat.HIGHSCORE:
        if tournament.score_ordering is None:
            return _needs_review()
        return PersonalGroup.ONGOING, lazy_gettext('Submit a score'), False
    if open_matches:
        if all(not m.opponents for m in open_matches):
            return (
                PersonalGroup.WAITING,
                lazy_gettext('Waiting for an opponent'),
                False,
            )
        return PersonalGroup.ONGOING, lazy_gettext('Open matches'), False
    if not own:
        return PersonalGroup.WAITING, lazy_gettext('Waiting for matches'), False
    if tournament.game_format == GameFormat.FREE_FOR_ALL:
        return _classify_ffa(tournament, own, matches, sides)
    if tournament.elimination_mode == EliminationMode.ROUND_ROBIN:
        return (
            PersonalGroup.FINISHED,
            lazy_gettext('All own matches completed'),
            False,
        )

    # Only leaf entries of the user's saved route decide their personal end.
    own_ids = {m.match.id for m in own}
    terminal = []
    for entry in own:
        match = entry.match
        contestants = sides.get(match.id, [])
        if len(contestants) == 1:
            winner = contestants[0]  # confirmed bye
        else:
            result = determine_match_winner(contestants)
            if result.is_err() or result.unwrap() is None:
                return _needs_review()
            winner = result.unwrap()
        won = winner.id == entry.own_side.id
        target = match.next_match_id if won else match.loser_next_match_id
        # Both grand-final contestants enter a saved reset, even its loser.
        if (
            match.bracket == Bracket.GRAND_FINAL
            and match.next_match_id in own_ids
        ):
            target = match.next_match_id
        if target in own_ids:
            continue
        if target is not None:
            # Confirmation materializes advancement atomically. Missing sides
            # are inconsistent data, not grounds to invent a future opponent.
            return _needs_review()
        terminal.append(won)
    if not terminal:
        return _needs_review()
    if terminal[-1]:
        return (
            PersonalGroup.FINISHED,
            lazy_gettext('All own matches completed'),
            False,
        )
    return PersonalGroup.FINISHED, lazy_gettext('Eliminated'), False


def _classify_ffa(tournament, own, matches, sides):
    # Pools have independent round numbers. Creation timestamps identify the
    # last materialized own round across WB/LB/GF without guessing from numbers.
    latest = max(own, key=lambda e: (e.match.created_at, _match_order(e.match)))
    match = latest.match
    gf = [m for m in matches if m.bracket == Bracket.GRAND_FINAL]
    if gf and match.bracket != Bracket.GRAND_FINAL:
        if match.bracket == Bracket.WINNERS:
            # The GF collector seeds every WB contestant. This fallback only
            # keeps a missing seat from reading as an elimination.
            return _needs_review()
        return PersonalGroup.FINISHED, lazy_gettext('Eliminated'), False
    if match.bracket == Bracket.GRAND_FINAL:
        return (
            PersonalGroup.FINISHED,
            lazy_gettext('All own matches completed'),
            False,
        )
    count = tournament.advancement_count
    if not count or latest.own_side.placement is None:
        return _needs_review()
    pool_round = [
        m
        for m in matches
        if m.bracket == match.bracket and m.round == match.round
    ]
    if any(m.confirmed_by is None for m in pool_round):
        return (
            PersonalGroup.WAITING,
            lazy_gettext('Waiting for round advancement'),
            False,
        )
    contestants = sorted(
        sides.get(match.id, []), key=lambda c: c.points or 0, reverse=True
    )
    if len(contestants) > count and (contestants[count - 1].points or 0) == (
        contestants[count].points or 0
    ):
        return (
            PersonalGroup.WAITING,
            lazy_gettext('Advancement cutoff tied'),
            True,
        )
    qualifies = latest.own_side.id in {c.id for c in contestants[:count]}
    if qualifies or match.bracket == Bracket.WINNERS:
        return (
            PersonalGroup.WAITING,
            lazy_gettext('Waiting for round advancement'),
            False,
        )
    return PersonalGroup.FINISHED, lazy_gettext('Eliminated'), False


def get_personal_overview(party_id: PartyID, user_id: UserID) -> dict:
    tournaments = tournament_service.get_tournaments_for_party(party_id)
    visible = {
        t.id: t
        for t in tournaments
        if t.tournament_status not in (None, TournamentStatus.DRAFT)
    }
    participants = repository.get_active_participations(party_id, user_id)
    participants = [p for p in participants if p.tournament_id in visible]
    teams = tournament_repository.get_teams_by_ids(
        {p.team_id for p in participants if p.team_id}
    )
    teams_by_id = {t.id: t for t in teams if t.removed_at is None}
    participants = [
        p
        for p in participants
        if p.team_id is None
        or (
            p.team_id in teams_by_id
            and teams_by_id[p.team_id].tournament_id == p.tournament_id
        )
    ]
    ids = [p.tournament_id for p in participants]
    highscore_types = {
        tid: derive_contestant_type(
            visible[tid].contestant_type,
            visible[tid].max_players_in_team,
            visible[tid].min_players_in_team,
        )
        for tid in ids
        if visible[tid].game_format == GameFormat.HIGHSCORE
        and visible[tid].tournament_status == TournamentStatus.ONGOING
    }
    complete_highscores = repository.get_highscores_with_complete_results(
        party_id, highscore_types
    )
    matches = repository.get_matches(ids)
    sides = tournament_repository.get_contestants_for_matches(
        [m.id for m in matches]
    )
    matches_by_tournament = defaultdict(list)
    for match in matches:
        matches_by_tournament[match.tournament_id].append(match)
    entries = [
        evaluate_participation(
            visible[p.tournament_id],
            p,
            matches_by_tournament[p.tournament_id],
            sides,
            highscore_results_complete=p.tournament_id in complete_highscores,
        )
        for p in participants
    ]
    entries.sort(
        key=lambda e: (
            e.tournament.position,
            str(e.tournament.id),
        )
    )
    opponents = repository.get_participants(ids)
    opponent_teams = tournament_repository.get_teams_by_ids(
        {c.team_id for cs in sides.values() for c in cs if c.team_id}
        - set(teams_by_id)
    )
    teams_by_id.update({t.id: t for t in opponent_teams})
    orgas = tournament_orga_service.get_public_orgas_for_tournaments(ids)
    contact_ids = {o.user.id for assigned in orgas.values() for o in assigned}
    opponent_participant_ids = {
        side.participant_id
        for entry in entries
        for encounter in entry.matches
        for side in encounter.opponents
        if side.participant_id
    }
    opponent_team_ids = {
        side.team_id
        for entry in entries
        for encounter in entry.matches
        for side in encounter.opponents
        if side.team_id
    }
    contact_ids.update(
        p.user_id for p in opponents if p.id in opponent_participant_ids
    )
    captain_ids = {
        t.captain_user_id
        for t in teams_by_id.values()
        if t.id in opponent_team_ids and t.removed_at is None
    }
    contact_ids.update(captain_ids)
    users = {o.user.id: o.user for assigned in orgas.values() for o in assigned}
    missing_user_ids = contact_ids - set(users)
    if missing_user_ids:
        users.update(user_service.get_users_indexed_by_id(missing_user_ids))
    return {
        **_contact_context(party_id, users),
        'entries': entries,
        'personal_groups': {
            group: [e for e in entries if e.group == group]
            for group in PersonalGroup
        },
        'orgas_by_tournament': orgas,
        'teams_by_id': teams_by_id,
        'participants_by_id': {
            p.id: users[p.user_id] for p in opponents if p.user_id in users
        },
    }


def get_supervised_overview(
    party_id: PartyID, user_id: UserID, *, include_all: bool = False
) -> dict:
    """The caller authorizes `include_all`; assignments never grant backend rights."""
    tournaments = tournament_service.get_tournaments_for_party(party_id)
    assigned = tournament_orga_service.get_tournament_ids_for_orga(user_id)
    tournaments = [t for t in tournaments if include_all or t.id in assigned]
    ids = [t.id for t in tournaments]
    orgas = tournament_orga_service.get_public_orgas_for_tournaments(ids)
    contact_users = {
        o.user.id: o.user for assigned in orgas.values() for o in assigned
    }
    return {
        **_contact_context(party_id, contact_users),
        'tournaments': tournaments,
        'orgas_by_tournament': orgas,
        'participant_counts': tournament_service.get_participant_counts_for_tournaments(
            ids
        ),
        'team_counts': tournament_repository.get_team_counts_for_tournaments(
            ids
        ),
    }


def _contact_context(party_id: PartyID, users: dict[UserID, User]) -> dict:
    """Profile links follow `user_profile.view`'s active-account contract.

    An assigned orga's actual seat is independent of profile availability.
    Deleted accounts never contribute contact or seat data.
    """
    public_users = {
        uid: user for uid, user in users.items() if not user.deleted
    }
    return {
        'contact_users_by_id': {
            uid: user
            for uid, user in public_users.items()
            if user.initialized and not user.suspended
        },
        'seats_by_user_id': repository.get_contact_seats(
            party_id, set(public_users)
        ),
    }
