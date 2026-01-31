from typing import List


from byceps.services.lan_tournament.models.tournament_match_comment import TournamentMatchComment, TournamentMatchCommentId
from byceps.services.user.models.user import UserID as UserId


from .models.tournament import TournamentId
from .models.tournament_match import TournamentMatch, TournamentMatchId
from .models.tournament_seed import TournamentSeed


def set_seed(seed_list: List[TournamentSeed], tournament_id: TournamentId) -> None:
    raise NotImplementedError


def start_tournament(tournament_id: TournamentId) -> None:
    raise NotImplementedError


def pause_tournament(tournament_id: TournamentId) -> None:
    raise NotImplementedError


def resume_tournament(tournament_id: TournamentId) -> None: 
    raise NotImplementedError


def end_tournament(tournament_id: TournamentId) -> None:
    raise NotImplementedError


def reset_match(match_id: TournamentMatchId) -> None:
    raise NotImplementedError


def get_match(match_id: TournamentMatchId) -> TournamentMatch:
    raise NotImplementedError


def get_matches_for_tournament(tournament_id: TournamentId) -> List[TournamentMatch]:
    raise NotImplementedError


def confirm_match(match_id: TournamentMatchId, confirmed_by_user_id: UserId) -> None:
    raise NotImplementedError


def set_score(match_id: TournamentMatchId, contestand_id: str, score: int
) -> None:
    raise NotImplementedError


def add_comment(comment: TournamentMatchComment)-> None:
    raise NotImplementedError


def update_comment(comment: TournamentMatchComment) -> None:
    raise NotImplementedError


def delete_comment(comment_id: TournamentMatchCommentId) -> None:
    raise NotImplementedError


def get_comments_from_match(match_id: TournamentMatchId) -> List[TournamentMatchComment]:
    raise NotImplementedError
