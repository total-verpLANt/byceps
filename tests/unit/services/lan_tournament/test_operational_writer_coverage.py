import ast
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / 'byceps/services/lan_tournament'
TESTS = ROOT / 'tests'

MATCH = 'tournament_match_service.py'
LIFECYCLE = 'tournament_service.py'
SEEDING = 'tournament_seeding_service.py'
QUALIFICATION = 'tournament_qualification_service.py'
PARTICIPANT = 'tournament_participant_service.py'
TEAM = 'tournament_team_service.py'
REPOSITORY = 'tournament_repository.py'
DASHBOARD_REPOSITORY = 'tournament_dashboard_repository.py'
COORDINATION = 'tournament_dashboard_coordination_service.py'
READINESS = 'tournament_readiness_service.py'
SITE_VIEWS = 'blueprints/site/views.py'

COMMIT_CALLS = {'commit', 'commit_session'}
STATEMENT_WRITERS = {'insert', 'pg_insert', 'update', 'delete'}
SESSION_WRITERS = {'add', 'add_all', 'merge', 'delete'}


@cache
def _trees() -> dict[str, ast.Module]:
    """Parse every module of the package that can hold a writer or an owner."""
    trees = {}
    for path in sorted(PACKAGE.rglob('*.py')):
        relative = path.relative_to(PACKAGE).as_posix()
        if relative.startswith(('dbmodels/', 'migrations/')):
            continue
        trees[relative] = ast.parse(path.read_text())
    return trees


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _function(
    relative: str, name: str, trees: dict[str, ast.Module] | None = None
) -> ast.FunctionDef:
    return _functions((trees or _trees())[relative])[name]


def _leaf(call: ast.Call) -> str:
    return ast.unparse(call.func).rpartition('.')[2]


def _calls(node: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _lines(node: ast.AST, names: set[str] | frozenset[str]) -> list[int]:
    """Return the source lines of the calls to `names`, in order."""
    return sorted(c.lineno for c in _calls(node) if _leaf(c) in names)


def _is_repository(relative: str) -> bool:
    return relative.endswith('_repository.py')


# -- scanners; each takes the trees, so a test can add a stranger --


def _writes_rows(function: ast.FunctionDef) -> bool:
    """Tell whether a function changes rows, by statement or by assignment."""
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            leaf = _leaf(node)
            receiver = ast.unparse(node.func).rpartition('.')[0]
            if leaf in STATEMENT_WRITERS:
                return True
            if leaf in SESSION_WRITERS and receiver.endswith('session'):
                return True
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            for leaf_node in ast.walk(target):
                if (
                    isinstance(leaf_node, ast.Attribute)
                    and isinstance(leaf_node.ctx, ast.Store)
                    and isinstance(leaf_node.value, ast.Name)
                ):
                    return True
    return False


def _repository_writers(trees: dict[str, ast.Module]) -> set[str]:
    return {
        name
        for name, function in _functions(trees[REPOSITORY]).items()
        if _writes_rows(function)
    }


def _commit_owners(trees: dict[str, ast.Module]) -> set[tuple[str, str]]:
    """Return every non-repository function that commits the session."""
    return {
        (relative, name)
        for relative, tree in trees.items()
        if not _is_repository(relative)
        for name, function in _functions(tree).items()
        if _lines(function, COMMIT_CALLS)
    }


def _callers(
    trees: dict[str, ast.Module], names: set[str]
) -> set[tuple[str, str]]:
    return {
        (relative, name)
        for relative, tree in trees.items()
        for name, function in _functions(tree).items()
        if _lines(function, names)
    }


def _dml(tree: ast.Module) -> list[tuple[int, str]]:
    """Return (line, what) of every write or commit in a module."""
    found = []
    for function in _functions(tree).values():
        if _writes_rows(function):
            found.append((function.lineno, function.name))
        for line in _lines(function, COMMIT_CALLS | {'flush'}):
            found.append((line, function.name))
    return found


# -- the manifest of repository writers --

# fmt: off
WRITER_KINDS = {
    # An actual change stamps `last_changed_at`.
    'stamps': {
        'create_match_contestant', 'confirm_match', 'unconfirm_match',
        'update_contestant_score', 'update_contestant_scores',
        'clear_contestant_scores', 'update_contestant_placement_and_points',
        'delete_match_contestant_flush', 'delete_contestant_from_match',
        'delete_contestants_for_match_flush',
        'remove_team_from_contestants_flush',
        'set_side_ready_flush', 'clear_side_ready_flush',
        '_touch_matches',
    },
    # A new match carries an initialized last change.
    'initializes': {'create_match'},
    # The one tournament-row writer of the status and the clock edge.
    'clock': {'set_tournament_status_flush'},
    # The matches go: their episodes close and their pins drop first.
    'deletes_matches': {'delete_match_flush', 'delete_matches_for_tournament'},
    # Deliberately unstamped: the whole tournament goes next, with every match.
    'deletes_tournament_entries': {'delete_contestants_for_tournament_flush'},
    # The episode, acknowledgement, pin and threshold tables.
    'episodes': {
        'open_due_episode_flush', 'close_due_episodes_flush',
        'retire_dashboard_matches_flush', 'advance_episode_ack_revision_flush',
    },
    'acks': {'create_escalation_ack_flush'},
    'pins': {'save_match_pin_flush'},
    'thresholds': {'set_party_thresholds_flush', 'delete_party_thresholds_flush'},
    # Routing is not a match change.
    'routing': {
        'clear_loser_next_match_id', 'clear_next_match_id',
        'set_next_match_id_flush', 'null_self_referential_fks',
    },
    # Each one is the consequence of a writer that stamps, or of a roster
    # change that is no match change.
    'readiness': {
        '_clear_match_readiness', 'clear_match_readiness_flush',
        'set_readiness_revision_flush', 'set_side_invitation_hold_flush',
        'refresh_match_pairing_flush', '_retire_match_pairing_flush',
        'set_both_ready_notified_flush', 'set_occupied_since_if_unset_flush',
        'mark_matches_both_ready_notified', 'suppress_match_invitations_flush',
    },
    # Notification work is never a match change.
    'invitations': {
        '_suppress_invitation', '_release_attempt', '_exhaust_invitation',
        'ensure_invitation_intents_flush', 'claim_invitation_dispatch_flush',
        'record_invitation_outcome_flush', 'recover_expired_invitations_flush',
    },
    'comments': {
        'create_match_comment', 'create_match_comment_flush',
        'update_match_comment', 'delete_match_comment',
        'delete_comments_for_match', 'delete_comments_for_match_flush',
        'delete_comments_for_tournament',
    },
    # Tournament-row writers with no match and no clock edge.
    'tournament_row': {
        'create_tournament', 'update_tournament', 'set_playoff_release',
        'set_playoff_elimination_mode', 'clear_playoff_release',
        'set_leaderboard_closed', 'delete_tournament',
        'clear_winner_for_tournament', 'clear_winner_team_reference',
        'clear_winner_participant_reference_flush', 'reorder_tournaments',
        'set_tournament_winner',
    },
    # Roster rows; the owners reconcile the demand they change.
    'roster': {
        'create_team', 'update_team', 'update_team_captain_flush',
        'delete_team', 'delete_team_flush', 'delete_teams_for_tournament_flush',
        'create_participant', 'update_participant_flush', 'delete_participant',
        'delete_participants_by_ids', 'delete_participants_for_tournament_flush',
        'remove_team_from_participants_flush', 'soft_delete_participants_by_ids',
        'soft_delete_team_flush', 'reactivate_participant',
    },
    # Leaderboard submissions and the audit purge touch no match.
    'unrelated': {
        'create_score_submission', 'delete_submissions_for_tournament',
        'delete_log_entries_older_than',
    },
}

BOUNDARY = ('unit', 'test_last_changed_repository.py', 'test_comments_and_annotations_do_not_touch')
ONLY_TOUCH = ('unit', 'test_last_changed_repository.py', 'test_only_creation_and_the_touch_helpers_write_the_last_change')
KIND_TESTS = {
    'stamps': [
        ('unit', 'test_last_changed_repository.py', 'test_actual_domain_writes_touch_last_changed'),
        ('integration', 'test_last_changed_repository.py', 'test_actual_domain_writes_touch_last_changed'),
    ],
    'initializes': [
        ('unit', 'test_operational_repository.py', 'test_create_match_initializes_with_the_server_operation_time'),
        ('integration', 'test_operational_repository.py', 'test_create_match_initializes_the_last_change'),
    ],
    'clock': [
        ('integration', 'test_operational_lifecycle.py', 'test_status_setter_follows_the_clock_edges'),
    ],
    'deletes_matches': [
        ('unit', 'test_tournament_deletion.py', 'test_match_deletion_retires_the_dashboard_state_before_the_row_goes'),
        ('integration', 'test_dashboard_history.py', 'test_regeneration_and_unrelease_do_not_block_on_annotations'),
        ('integration', 'test_dashboard_history.py', 'test_tournament_deletion_retains_snapshot_history'),
        ('unit', 'test_tournament_deletion.py', 'test_the_bracket_generators_hand_their_operation_time_to_the_preamble'),
        ('unit', 'test_tournament_deletion.py', 'test_the_preamble_hands_its_operation_time_to_the_clearing'),
    ],
    'deletes_tournament_entries': [
        ('integration', 'test_dashboard_history.py', 'test_tournament_deletion_retains_snapshot_history'),
    ],
    'episodes': [
        ('unit', 'test_operational_repository.py', 'test_only_one_open_episode_per_match'),
        ('integration', 'test_operational_repository.py', 'test_only_one_open_episode_per_match'),
    ],
    'acks': [
        ('unit', 'test_operational_repository.py', 'test_acks_are_episode_revision_unique'),
        ('unit', 'test_dashboard_acknowledgement.py', 'test_ack_audit_failure_rolls_back_every_fact'),
    ],
    'pins': [
        ('unit', 'test_dashboard_pins.py', 'test_pin_cannot_change_match_clock_or_last_change'),
    ],
    'thresholds': [
        ('unit', 'test_operational_repository.py', 'test_party_threshold_row_checks_and_cas'),
    ],
    'routing': [BOUNDARY],
    'readiness': [BOUNDARY],
    'invitations': [ONLY_TOUCH],
    'comments': [BOUNDARY],
    'tournament_row': [ONLY_TOUCH],
    'roster': [
        ('unit', 'test_removal_timing.py', 'test_membership_only_change_affects_demand_not_match_timestamp'),
    ],
    'unrelated': [ONLY_TOUCH],
}
# fmt: on


def _writer_kinds() -> dict[str, str]:
    found: dict[str, str] = {}
    for kind, names in WRITER_KINDS.items():
        for name in names:
            assert name not in found, (name, found[name], kind)
            found[name] = kind
    return found


def _test_exists(layer: str, filename: str, name: str) -> bool:
    path = TESTS / layer / 'services/lan_tournament' / filename
    if not path.exists():
        return False

    return name in {
        node.name
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.FunctionDef)
    }


# -- the manifest of owners --

# fmt: off
HOOK_GROUPS = {
    'time': {'_begin_operation', '_operation_time_of', 'get_operation_time'},
    'status': {'set_tournament_status_flush'},
    'invalidate': {'_invalidate_timing_flush', 'invalidate_due_matches_flush'},
    'retire': {'_retire_phase_two_flush', 'retire_dashboard_matches_flush'},
    'occupancy': {'_mark_lobbies_occupied_flush', 'mark_completed_lobbies_occupied_flush'},
    'reconcile': {
        '_reconcile_timing_flush', 'reconcile_due_matches_flush',
        '_reconcile_roster_timing_flush',
    },
}


@dataclass(frozen=True)
class Owner:
    """A commit owner whose changes the timing facts must follow.

    `needs` are the hook groups, in the order they must first be called in
    the function that holds them (the `site`, default the owner itself).
    The owner reaches its site before every commit, or before the call of
    its `committer`. `reaches` are groups that a callee must hold, in no
    order. `tests` are the behavioral tests of the owner.
    """

    needs: tuple[str, ...]
    tests: tuple[tuple[str, str, str], ...]
    site: tuple[str, str] | None = None
    committer: str | None = None
    reaches: tuple[str, ...] = ()


RESULT = ('integration', 'test_result_timing.py', 'test_every_result_owner_uses_one_operation_time')
RESULT_ATOMIC = ('unit', 'test_result_timing_atomicity.py', 'test_result_timing_flush_precedes_commit_and_signals')
FFA_RESULT = ('unit', 'test_ffa_timing.py', 'test_every_ffa_result_owner_reconciles_before_its_commit')
FFA_ROUNDS = ('unit', 'test_ffa_timing.py', 'test_wb_and_companion_lb_timing_is_atomic')
PHASE = ('unit', 'test_phase_timing.py', 'test_generation_samples_one_time_and_reconciles_before_the_commit')
REMOVAL = ('unit', 'test_removal_timing.py', 'test_every_removal_owner_reconciles_or_is_a_self_service_owner')
LIFECYCLE_TEST = ('integration', 'test_operational_lifecycle.py', 'test_preassigned_matches_begin_at_actual_start')
CORRECTION = ('unit', 'test_result_timing_atomicity.py', 'test_a_correction_invalidates_the_downstream_before_the_retraction')

OWNERS = {
    (MATCH, 'admin_set_and_confirm_match'): Owner(('time', 'reconcile'), (RESULT, RESULT_ATOMIC)),
    (MATCH, 'complete_settled_plain_round_robin'): Owner(('reconcile',), (RESULT_ATOMIC,)),
    (MATCH, 'confirm_match'): Owner(('time', 'reconcile'), (RESULT, RESULT_ATOMIC)),
    (MATCH, 'unconfirm_match'): Owner(('time', 'reconcile'), (RESULT, RESULT_ATOMIC)),
    (MATCH, '_correct_result_in_place'): Owner(('reconcile',), (RESULT_ATOMIC,)),
    (MATCH, 'correct_match_result'): Owner(('time', 'invalidate', 'reconcile'), (CORRECTION, RESULT)),
    (MATCH, 'generate_ffa_round'): Owner(('time', 'reconcile'), (FFA_ROUNDS,), reaches=('occupancy',)),
    (MATCH, 'set_ffa_placements'): Owner(('time', 'reconcile'), (FFA_RESULT,)),
    (MATCH, 'confirm_ffa_match'): Owner(('time', 'reconcile'), (FFA_RESULT,)),
    (MATCH, 'set_and_confirm_ffa_match'): Owner(('time', 'reconcile'), (FFA_RESULT,)),
    (MATCH, 'advance_ffa_round'): Owner(('time', 'reconcile'), (FFA_ROUNDS,), reaches=('occupancy',)),
    (MATCH, 'generate_ffa_grand_final'): Owner(('time', 'occupancy', 'reconcile'), (FFA_ROUNDS,)),
    (LIFECYCLE, 'change_status'): Owner(('status', 'reconcile'), (LIFECYCLE_TEST,), site=(LIFECYCLE, '_persist_status_change_flush')),
    (SEEDING, 'generate_from_seeding'): Owner(('time', 'reconcile'), (PHASE,), site=(SEEDING, '_generate_locked')),
    (SEEDING, 'prepare_ffa_round_draft'): Owner(('time', 'reconcile'), (PHASE,), site=(SEEDING, '_complete_single_survivor_flush')),
    (QUALIFICATION, 'try_auto_release'): Owner(('time', 'reconcile'), (PHASE,), site=(SEEDING, '_generate_locked')),
    (QUALIFICATION, 'release_playoffs'): Owner(('time', 'reconcile'), (PHASE,), site=(SEEDING, '_generate_locked'), committer='_finish_release'),
    (QUALIFICATION, 'unrelease_playoffs'): Owner(('time', 'retire', 'reconcile'), (('unit', 'test_phase_timing.py', 'test_unrelease_retains_history_and_removes_live_annotations'),), site=(QUALIFICATION, '_unrelease_locked')),
    (PARTICIPANT, 'admin_remove_participant'): Owner(('time', 'reconcile'), (REMOVAL,)),
    (PARTICIPANT, 'remove_participants_without_tickets'): Owner(('time', 'reconcile'), (REMOVAL,)),
    (TEAM, 'delete_team'): Owner(('time', 'reconcile'), (REMOVAL,)),
    (TEAM, 'remove_team_member'): Owner(('time', 'reconcile'), (REMOVAL,)),
}

# A commit owner that changes no timing fact, or whose hook is held by the
# owner that calls it, with the reason.
NO_TIMING = {
    (MATCH, 'generate_single_elimination_bracket'): 'no production caller; generation goes through seeding, which reconciles',
    (MATCH, 'generate_double_elimination_bracket'): 'no production caller; generation goes through seeding, which reconciles',
    (MATCH, 'generate_round_robin_bracket'): 'no production caller; generation goes through seeding, which reconciles',
    (MATCH, 'set_score'): 'an unconfirmed score changes no demand; the writer stamps',
    (MATCH, 'delete_match'): 'deletion; the repository retires the dashboard state',
    (LIFECYCLE, 'create_tournament'): 'a new tournament has no matches and no history',
    (LIFECYCLE, 'delete_tournament'): 'deletion; the repository retires the dashboard state',
    (QUALIFICATION, '_finish_release'): 'commit helper of `release_playoffs`, which holds the hook',
    (QUALIFICATION, 'save_decision'): 'a decision changes no match',
    (QUALIFICATION, 'withdraw_decision'): 'a decision changes no match',
    (SEEDING, 'apply_action'): 'a draft holds no match',
    (SEEDING, 'ensure_playoff_draft'): 'a draft holds no match',
    (SEEDING, '_create_draft'): 'a draft holds no match',
    (PARTICIPANT, 'join_tournament'): 'membership only; refused once the tournament started',
    (PARTICIPANT, 'admin_add_participant'): 'membership only',
    (PARTICIPANT, 'leave_tournament'): 'self service, refused once the tournament started',
    (TEAM, 'create_team'): 'membership only',
    (TEAM, 'join_team'): 'membership only; no match fact changes',
    (TEAM, 'leave_team'): 'self service, refused once the tournament started',
    (TEAM, 'transfer_captain'): 'membership only',
    (TEAM, 'admin_add_member'): 'membership only',
    ('tournament_orga_service.py', 'assign_orga'): 'orga assignment',
    ('tournament_orga_service.py', 'revoke_orga'): 'orga assignment',
    ('tournament_score_service.py', 'close_leaderboard'): 'leaderboard, no match',
    ('tournament_score_service.py', 'reopen_leaderboard'): 'leaderboard, no match',
    (COORDINATION, 'set_match_pin'): 'annotation: no clock, no last change, no episode',
    (COORDINATION, '_acknowledge'): 'annotation: no clock, no last change, no occupancy',
    ('tournament_dashboard_settings_service.py', 'set_party_thresholds'): 'thresholds recolour; episodes and acknowledgements stay',
    ('tournament_dashboard_settings_service.py', 'reset_party_thresholds'): 'thresholds recolour; episodes and acknowledgements stay',
    ('tournament_image_service.py', 'store_uploaded_image'): 'images',
    ('tournament_image_service.py', 'delete_staged_image'): 'images',
    ('tournament_maintenance_service.py', 'delete_unused_images'): 'images',
    ('tournament_invitation_service.py', '_dispatch_batch'): 'notification work, not a match change',
    ('tournament_invitation_service.py', '_sweep_once'): 'notification work, not a match change',
    ('tournament_invitation_service.py', '_record_outcome'): 'notification work, not a match change',
    ('tournament_log_service.py', 'persist_log_entry'): 'the audit log is no clock authority',
    ('tournament_request_service.py', 'submit_request'): 'tournament requests',
    ('tournament_request_service.py', 'update_request'): 'tournament requests',
    ('tournament_request_service.py', 'withdraw_request'): 'tournament requests',
    ('tournament_request_service.py', 'accept_request'): 'tournament requests',
    ('tournament_request_service.py', 'reject_request'): 'tournament requests',
    (SITE_VIEWS, '_mutate_readiness'): 'Ready: the repository setters stamp, see `test_the_ready_owner_commits_after_the_stamping_services`',
}
# fmt: on


def _hands_down(call: ast.Call, value: str) -> bool:
    """Tell whether the call spreads `value` as keywords (`**value`)."""
    return any(
        keyword.arg is None and ast.unparse(keyword.value) == value
        for keyword in call.keywords
    )


def _passes_time(call: ast.Call) -> bool:
    """Tell whether the call hands the owner's `changed_at` down."""
    return _hands_down(call, '_changed(changed_at)') or any(
        keyword.arg == 'changed_at'
        and ast.unparse(keyword.value) == 'changed_at'
        for keyword in call.keywords
    )


def _first_lines(node: ast.AST, groups: tuple[str, ...]) -> list[list[int]]:
    return [_lines(node, HOOK_GROUPS[group]) for group in groups]


def _graph(trees: dict[str, ast.Module]) -> dict[str, set[str]]:
    """Map each service function name to the names it calls."""
    graph: dict[str, set[str]] = {}
    for relative, tree in trees.items():
        if _is_repository(relative):
            continue
        for name, function in _functions(tree).items():
            graph.setdefault(name, set()).update(
                _leaf(c) for c in _calls(function)
            )
    return graph


def _reaches(graph: dict[str, set[str]], start: str, targets: set[str]) -> bool:
    seen, stack = set(), [start]
    while stack:
        name = stack.pop()
        if name in seen:
            continue

        seen.add(name)
        called = graph.get(name, set())
        if called & targets:
            return True

        stack.extend(called)
    return False


def _reach_lines(
    owner: ast.FunctionDef, graph: dict[str, set[str]], targets: set[str]
) -> list[int]:
    """Return the lines of the calls in `owner` that lead to `targets`."""
    return sorted(
        c.lineno
        for c in _calls(owner)
        if _leaf(c) in targets or _reaches(graph, _leaf(c), targets)
    )


def _owner_problems(
    trees: dict[str, ast.Module], relative: str, name: str, spec: Owner
) -> list[str]:
    """Return what keeps an owner from running its hooks before its commit."""
    owner = _function(relative, name, trees)
    graph = _graph(trees)
    site_relative, site_name = spec.site or (relative, name)
    site = _function(site_relative, site_name, trees)
    problems = []

    firsts = _first_lines(site, spec.needs)
    for group, lines in zip(spec.needs, firsts, strict=True):
        if not lines:
            problems.append(f'{site_name} never calls a `{group}` hook')
    if problems:
        return problems

    starts = [lines[0] for lines in firsts]
    if starts != sorted(set(starts)):
        problems.append(f'{site_name} calls its hooks out of order: {starts}')

    if spec.committer is not None:
        commits = _lines(owner, {spec.committer})
    else:
        commits = _lines(owner, COMMIT_CALLS)
    if not commits:
        problems.append(f'{name} has no commit to precede')
        return problems

    if site is owner:
        before = firsts[-1]
    else:
        before = _reach_lines(owner, graph, {site_name})
    for commit in commits:
        if not any(line < commit for line in before):
            problems.append(f'{name} commits at {commit} before its hooks')

    for group in spec.reaches:
        if not _reach_lines(owner, graph, HOOK_GROUPS[group]):
            problems.append(f'{name} never reaches a `{group}` hook')
    return problems


def _every_test_pointer() -> list[tuple[str, str, str]]:
    pointers = [p for tests in KIND_TESTS.values() for p in tests]
    pointers += [p for spec in OWNERS.values() for p in spec.tests]
    return pointers


# -- the tests --


def test_domain_mutation_manifest_is_complete():
    kinds = _writer_kinds()
    found = _repository_writers(_trees())

    assert found - kinds.keys() == set(), 'writers missing from the manifest'
    assert kinds.keys() - found == set(), 'manifest entries without a writer'
    assert set(WRITER_KINDS) == set(KIND_TESTS)

    repository = _functions(_trees()[REPOSITORY])
    touch = {
        '_touch_matches',
        'touch_matches_last_changed_flush',
        '_touch_changed_matches_flush',
        '_touch_match_if',
    }
    stamping = {
        name for name, function in repository.items() if _lines(function, touch)
    }
    assert stamping == WRITER_KINDS['stamps'] - {'_touch_matches'} | (
        touch - {'_touch_matches'}
    )

    # Only the status setter edges the clock, and only it and its creation
    # twin store the clock columns.
    clock = {
        name
        for name, function in repository.items()
        if 'operational_clock' in ast.unparse(function)
        and 'DbTournament' in ast.unparse(function)
        and _writes_rows(function)
    }
    assert clock <= WRITER_KINDS['clock'] | {'create_tournament'}

    # Every kind names behavioral tests, and each of them exists.
    missing = [p for p in _every_test_pointer() if not _test_exists(*p)]
    assert missing == []

    # Coordination writes only through their own three writers, and the
    # coordination service is their only caller.
    coordination = {
        'save_match_pin_flush',
        'create_escalation_ack_flush',
        'advance_episode_ack_revision_flush',
    }
    assert _callers(_trees(), coordination) == {
        (COORDINATION, 'set_match_pin'),
        (COORDINATION, '_acknowledge'),
    }

    # An annotation moves no clock, no last change and no episode of a match.
    moving = (
        WRITER_KINDS['stamps']
        | HOOK_GROUPS['status']
        | HOOK_GROUPS['reconcile']
        | HOOK_GROUPS['invalidate']
        | HOOK_GROUPS['occupancy']
        | {
            'touch_matches_last_changed_flush',
            'open_due_episode_flush',
            'close_due_episodes_flush',
            'retire_dashboard_matches_flush',
        }
    )
    for name, function in _functions(_trees()[COORDINATION]).items():
        assert _lines(function, moving) == [], name

    # The dashboard read side holds no writer, no flush and no commit.
    if DASHBOARD_REPOSITORY in _trees():
        assert _dml(_trees()[DASHBOARD_REPOSITORY]) == []


def test_status_and_commit_owners_have_precommit_hooks():
    owners = _commit_owners(_trees())
    classified = OWNERS.keys() | NO_TIMING.keys()

    assert owners - classified == set(), (
        'commit owners without a manifest entry'
    )
    # An owner that commits through a helper does not call the commit itself.
    delegating = {key for key, spec in OWNERS.items() if spec.committer}
    assert classified - owners - delegating == set(), (
        'manifest entries that no longer commit'
    )
    assert not OWNERS.keys() & NO_TIMING.keys()

    problems = {
        key: found
        for key, spec in OWNERS.items()
        if (found := _owner_problems(_trees(), *key, spec))
    }
    assert problems == {}

    # The commit helper has one caller, the owner that holds the hook.
    assert _callers(_trees(), {'_finish_release'}) == {
        (QUALIFICATION, 'release_playoffs')
    }

    # Every status writer is a pinned adapter, and the automatic ones take
    # the operation time of their owner.
    status = _callers(_trees(), HOOK_GROUPS['status'])
    assert {name for relative, name in status if relative == MATCH} == {
        '_try_auto_complete_tournament',
        'try_complete_plain_round_robin',
        '_unconfirm_match_impl',
        'complete_ffa_single_survivor',
    }
    assert {name for relative, name in status if relative != MATCH} == {
        '_persist_status_change_flush'
    }
    for name in (
        '_try_auto_complete_tournament',
        'try_complete_plain_round_robin',
        '_unconfirm_match_impl',
        'complete_ffa_single_survivor',
    ):
        setter = [
            call
            for call in _calls(_function(MATCH, name))
            if _leaf(call) in HOOK_GROUPS['status']
        ]
        assert setter
        for call in setter:
            assert any(
                keyword.arg is None
                and ast.unparse(keyword.value) == '_changed(changed_at)'
                for keyword in call.keywords
            ), (name, call.lineno)

    missing = [p for p in _every_test_pointer() if not _test_exists(*p)]
    assert missing == []


def test_the_generators_hand_one_operation_time_to_every_writer():
    # The regeneration of a bracket runs in the owner's one operation: the
    # clearing, the creation, the bye advances and the clearing's cleanup.
    writers = {
        'create_match',
        '_create_match_contestant_flush',
        'confirm_match',
        '_prepare_bracket_generation',
    }
    for name in (
        '_generate_single_elimination_impl',
        '_generate_double_elimination_impl',
        '_generate_round_robin_impl',
    ):
        calls = [
            call
            for call in _calls(_function(MATCH, name))
            if _leaf(call) in writers
        ]
        assert {_leaf(call) for call in calls} >= {
            'create_match',
            '_create_match_contestant_flush',
            '_prepare_bracket_generation',
        }, name
        for call in calls:
            assert _hands_down(call, '_changed(changed_at)'), (
                name,
                call.lineno,
            )

    prepare = [
        call
        for call in _calls(_function(MATCH, '_prepare_bracket_generation'))
        if _leaf(call) == 'clear_bracket'
    ]
    assert prepare and all(
        _hands_down(call, '_changed(changed_at)') for call in prepare
    )

    # The seeding generator hands its time to every generator.
    generators = [
        call
        for call in _calls(_function(SEEDING, '_run_generator'))
        if _leaf(call).startswith('_generate_')
    ]
    assert len(generators) == 5
    for call in generators:
        assert _hands_down(call, 'stamp'), (_leaf(call), call.lineno)

    # The un-release hands its time to the clearing of the second phase.
    clearing = [
        call
        for call in _calls(_function(QUALIFICATION, '_unrelease_locked'))
        if _leaf(call) == 'clear_bracket'
    ]
    assert clearing and all(
        _hands_down(call, 'tournament_match_service._changed(changed_at)')
        for call in clearing
    )


def test_the_ready_owner_commits_after_the_stamping_services():
    owner = _function(SITE_VIEWS, '_mutate_readiness')
    services = _lines(owner, {'claim_ready_flush', 'revoke_ready_flush'})
    commits = _lines(owner, COMMIT_CALLS)

    assert services and commits
    assert all(min(services) < commit for commit in commits)

    assert _lines(
        _function(READINESS, 'claim_ready_flush'), {'set_side_ready_flush'}
    )
    assert _lines(
        _function(READINESS, 'revoke_ready_flush'), {'clear_side_ready_flush'}
    )
    assert {'set_side_ready_flush', 'clear_side_ready_flush'} <= WRITER_KINDS[
        'stamps'
    ]
    assert _callers(
        _trees(), {'set_side_ready_flush', 'clear_side_ready_flush'}
    ) == {
        (READINESS, 'claim_ready_flush'),
        (READINESS, 'revoke_ready_flush'),
    }


def test_destructive_paths_have_episode_invalidation():
    repository = _functions(_trees()[REPOSITORY])

    # Each deletion writer retires the dashboard state before its DELETE.
    helper = repository['_retire_dashboard_state_flush']
    assert _lines(helper, {'retire_dashboard_matches_flush'})
    for name in sorted(WRITER_KINDS['deletes_matches']):
        function = repository[name]
        retire = _lines(function, {'_retire_dashboard_state_flush'})
        deletes = [
            call.lineno
            for call in _calls(function)
            if _leaf(call) == 'execute'
            and 'delete(DbTournamentMatch)' in ast.unparse(call)
        ]
        assert retire and deletes, name
        assert max(retire) < min(deletes), name

    # The retirement closes episodes and drops pins, and keeps the history.
    retire_source = ast.unparse(repository['retire_dashboard_matches_flush'])
    assert 'closed_at' in retire_source
    assert 'DbMatchDashboardAnnotation' in retire_source
    for name, function in repository.items():
        for table in ('DbMatchDueEpisode', 'DbMatchEscalationAck'):
            deletes_history = any(
                _leaf(call) == 'delete' and table in ast.unparse(call)
                for call in _calls(function)
            )
            assert not deletes_history, (name, table)

    # The only writers of the episode and acknowledgement tables.
    touching = {
        table: {
            name
            for name, function in repository.items()
            if table in ast.unparse(function) and _writes_rows(function)
        }
        for table in ('DbMatchDueEpisode', 'DbMatchEscalationAck')
    }
    assert touching['DbMatchDueEpisode'] == WRITER_KINDS['episodes']
    assert touching['DbMatchEscalationAck'] == WRITER_KINDS['acks']

    # Every caller of a deletion writer is a known owner.
    assert _callers(_trees(), {'delete_match_flush'}) == {
        (MATCH, 'clear_bracket'),
        (MATCH, '_unconfirm_match_impl'),
        (MATCH, 'delete_match'),
        (MATCH, '_delete_matches_flush'),
    } | {(REPOSITORY, 'delete_match')}
    assert _callers(_trees(), {'delete_matches_for_tournament'}) == {
        (LIFECYCLE, 'delete_tournament')
    }

    # The owners of a tracked tournament hand their operation time down, so
    # that the episodes close at it, not at a later reading.
    for name in (
        'clear_bracket',
        '_delete_matches_flush',
        '_unconfirm_match_impl',
    ):
        deleting = [
            call
            for call in _calls(_function(MATCH, name))
            if _leaf(call)
            in {'delete_match_flush', '_delete_contestants_for_match_flush'}
        ]
        assert {_leaf(call) for call in deleting} == {
            'delete_match_flush',
            '_delete_contestants_for_match_flush',
        }, name
        for call in deleting:
            assert _passes_time(call), (name, call.lineno)

    # A correction closes the episodes before it retracts the downstream.
    correction = _function(MATCH, 'correct_match_result')
    invalidate = _lines(correction, HOOK_GROUPS['invalidate'])
    retract = _lines(correction, {'_unconfirm_match_flush'})
    assert invalidate and retract
    assert max(invalidate) < min(retract)

    # An un-release retires the second phase before it clears the bracket.
    unrelease = _function(QUALIFICATION, '_unrelease_locked')
    assert max(_lines(unrelease, {'_retire_phase_two_flush'})) < min(
        _lines(unrelease, {'clear_bracket'})
    )

    # The grand final reset retires its match before it deletes it.
    reset = _function(MATCH, '_unconfirm_match_impl')
    assert min(_lines(reset, {'retire_dashboard_matches_flush'})) < min(
        _lines(reset, {'delete_match_flush'})
    )


# fmt: off
STRANGERS = {
    'statement on a match':
        'def new_writer(match_id):\n    db.session.execute(update(DbTournamentMatch).values(x=1))',
    'attribute of a row':
        'def new_writer(row):\n    row.confirmed_by = None',
    'session add':
        'def new_writer(row):\n    db.session.add(row)',
    'delete statement':
        'def new_writer(tournament_id):\n    db.session.execute(delete(DbTournamentMatch))',
    'augmented assignment':
        'def new_writer(row):\n    row.revision += 1',
}
# fmt: on


@pytest.mark.parametrize('name', list(STRANGERS))
def test_new_uncovered_writer_is_detected(name):
    trees = dict(_trees())
    stranger = ast.parse(STRANGERS[name])
    trees[REPOSITORY] = ast.Module(
        body=[*trees[REPOSITORY].body, *stranger.body], type_ignores=[]
    )

    uncovered = _repository_writers(trees) - _writer_kinds().keys()

    assert uncovered == {'new_writer'}


def test_new_uncovered_commit_owner_is_detected():
    trees = dict(_trees())
    trees['tournament_new_service.py'] = ast.parse(
        'def new_owner():\n    tournament_repository.commit_session()'
    )

    uncovered = _commit_owners(trees) - OWNERS.keys() - NO_TIMING.keys()

    assert uncovered == {('tournament_new_service.py', 'new_owner')}


def test_new_caller_of_a_deletion_writer_is_detected():
    trees = dict(_trees())
    trees['tournament_new_service.py'] = ast.parse(
        'def new_deleter(match_id):\n    tournament_repository.delete_match_flush(match_id)'
    )

    callers = _callers(trees, {'delete_match_flush'})

    assert ('tournament_new_service.py', 'new_deleter') in callers


# fmt: off
@pytest.mark.parametrize('source', [
    'def read(session):\n    session.add(1)',
    'def read(match):\n    match.revision = 1',
    'def read(session):\n    session.execute(delete(x))',
    'def read(session):\n    session.flush()',
    'def read(session):\n    session.commit()',
])
# fmt: on
def test_new_write_in_the_dashboard_read_side_is_detected(source):
    assert _dml(ast.parse(source)) != []


def test_the_dashboard_read_side_is_found_by_the_scanner():
    assert _dml(ast.parse('def read(session):\n    return session.scalars(1)')) == []
    assert DASHBOARD_REPOSITORY in _trees()


def _owner(*lines: str) -> str:
    return '\n'.join(lines) + '\n'


COMMIT = '    tournament_repository.commit_session()'
TIME = '    changed_at = _operation_time_of(t)'
RECONCILE = '    _reconcile_timing_flush(t, changed_at)'

# fmt: off
OWNER_STRANGERS = {
    'hook before the commit': (
        _owner('def owner(t):', TIME, RECONCILE, COMMIT), False,
    ),
    'commit before the hook': (
        _owner('def owner(t):', TIME, COMMIT, RECONCILE), True,
    ),
    'no reconcile': (
        _owner('def owner(t):', TIME, COMMIT), True,
    ),
    'reconcile before the time': (
        _owner('def owner(t):', '    _reconcile_timing_flush(t, None)', TIME, COMMIT), True,
    ),
    'second commit branch without a hook': (
        _owner(
            'def owner(t, ok):', TIME, '    if ok:', '    ' + COMMIT,
            RECONCILE, COMMIT,
        ), True,
    ),
}
# fmt: on


@pytest.mark.parametrize('case', list(OWNER_STRANGERS))
def test_an_owner_that_misplaces_its_hooks_is_detected(case):
    source, misplaced = OWNER_STRANGERS[case]
    trees = {'tournament_new_service.py': ast.parse(source)}
    spec = Owner(('time', 'reconcile'), ())

    problems = _owner_problems(
        trees, 'tournament_new_service.py', 'owner', spec
    )

    assert bool(problems) == misplaced, problems


def test_the_manifest_has_no_stale_test_pointer():
    assert [p for p in _every_test_pointer() if not _test_exists(*p)] == []
    assert not _test_exists(
        'unit', 'test_operational_writer_coverage.py', 'test_nothing'
    )
