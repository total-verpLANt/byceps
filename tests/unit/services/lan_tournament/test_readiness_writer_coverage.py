"""Pin engine writer ownership, not merely post-commit signal coverage."""

import ast
from datetime import datetime
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from byceps.services.lan_tournament import tournament_match_service as engine
from byceps.services.lan_tournament import tournament_readiness_service as readiness
from byceps.services.lan_tournament import tournament_repository as repository
from byceps.services.lan_tournament.dbmodels import (
    dashboard as dashboard_dbmodels,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.match_readiness import (
    DbMatchInvitation,
    DbMatchPairing,
)
from byceps.util.result import Err, Ok
from byceps.util.uuid import uuid7


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / 'byceps/services/lan_tournament'


def _functions(filename):
    tree = ast.parse((MODULE / filename).read_text())
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _calls(node):
    return {ast.unparse(call.func) for call in ast.walk(node) if isinstance(call, ast.Call)}


def _references(tree, symbols, scope=()):
    """Include aliases, indirect function references and string-based writes."""
    if isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        scope = (*scope, tree.name)
    name = None
    if isinstance(tree, ast.Name):
        name = tree.id
    elif isinstance(tree, ast.Attribute):
        name = tree.attr
    elif isinstance(tree, ast.keyword):
        name = tree.arg
    elif isinstance(tree, ast.alias):
        name = tree.name.rsplit('.', 1)[-1]
    elif isinstance(tree, ast.Constant) and isinstance(tree.value, str):
        name = tree.value
    elif isinstance(tree, ast.arg):
        name = tree.arg
    if name in symbols:
        yield scope, name, tree.lineno
    for child in ast.iter_child_nodes(tree):
        yield from _references(child, symbols, scope)


def test_legacy_marker_references_are_compatibility_only_across_application():
    marker = 'both_ready_notified_at'
    compatibility = {
        'set_both_ready_notified_flush',
        'get_both_ready_unnotified_match_ids',
        'mark_matches_both_ready_notified',
    }
    expected = {
        ('services/lan_tournament/dbmodels/match.py', ('DbTournamentMatch',)),
        ('services/lan_tournament/dbmodels/match.py', ('DbTournamentMatch', '__init__')),
        ('services/lan_tournament/models/tournament_match.py', ('TournamentMatch',)),
        ('services/lan_tournament/tournament_repository.py', ('_db_match_to_match',)),
        *(('services/lan_tournament/tournament_repository.py', (name,))
          for name in compatibility),
    }
    actual = set()
    application = ROOT / 'byceps'
    for path in sorted(application.rglob('*.py')):
        tree = ast.parse(path.read_text())
        for scope, name, line in _references(tree, {marker, *compatibility}):
            location = (path.relative_to(application).as_posix(), scope)
            # Definitions are not references. Imports, aliasing, direct calls,
            # attribute access and getattr-style string references all are.
            assert name == marker, (location, name, line)
            assert location in expected, (location, name, line)
            actual.add(location)
    assert actual == expected
    functions = _functions('tournament_repository.py')
    assert compatibility <= functions.keys()


# fmt: off
@pytest.mark.parametrize('source', [
    'row.both_ready_notified_at = None',
    'row.both_ready_notified_at += 1',
    'update(Model).values(both_ready_notified_at=None)',
    'setattr(row, "both_ready_notified_at", None)',
    'values = {"both_ready_notified_at": None}',
    'writer = repo.set_both_ready_notified_flush',
    'from module import mark_matches_both_ready_notified as writer',
    'getattr(repo, "get_both_ready_unnotified_match_ids")()',
])
# fmt: on
def test_marker_reference_scan_detects_writes_and_indirect_callers(source):
    assert list(_references(ast.parse(source), {
        'both_ready_notified_at', 'set_both_ready_notified_flush',
        'mark_matches_both_ready_notified', 'get_both_ready_unnotified_match_ids',
    }))


# fmt: off
@pytest.mark.parametrize('marker', [None, datetime(2026, 10, 1)])
@pytest.mark.parametrize('increment_revision', [False, True])
# fmt: on
def test_operational_clear_preserves_legacy_marker_and_pair_facts(marker, increment_revision):
    now = datetime(2026, 10, 6)
    row = SimpleNamespace(
        ready_at_a=now, ready_at_b=now, ready_by_a='a', ready_by_b='b',
        invitation_hold_a=True, invitation_hold_b=True,
        both_ready_notified_at=marker, occupied_since=now,
        pairing_id='pair', pairing_generation=7, readiness_revision=11,
    )
    session = Mock()
    session.get.return_value = row
    with patch.object(repository.db, 'session', session):
        repository.clear_match_readiness_flush('match', increment_revision=increment_revision)
    assert row.both_ready_notified_at == marker
    assert row.occupied_since == now
    assert row.pairing_id == 'pair' and row.pairing_generation == 7
    assert row.readiness_revision == 11 + int(increment_revision)
    assert row.ready_at_a is row.ready_at_b is row.ready_by_a is row.ready_by_b is None
    assert row.invitation_hold_a is row.invitation_hold_b is False
    session.get.assert_called_once_with(repository.DbTournamentMatch, 'match')
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()
    session.execute.assert_not_called()


# fmt: off
@pytest.mark.parametrize('marker', [None, datetime(2026, 10, 1)])
# fmt: on
def test_retained_marker_constructor_column_and_mapper_roundtrip(marker):
    row = repository.DbTournamentMatch(
        uuid7(), uuid7(), datetime(2026, 10, 6), both_ready_notified_at=marker,
    )
    dto = repository._db_match_to_match(row)
    assert row.both_ready_notified_at == dto.both_ready_notified_at == marker
    column = repository.DbTournamentMatch.__table__.c.both_ready_notified_at
    assert column.nullable
    assert dto.__dataclass_params__.frozen


def test_current_roster_and_generation_adapters_are_covered():
    manifest = {
        'tournament_participant_service.py': {
            'tournament_readiness_service.refresh_pairing_and_invitations_flush': {
                '_refresh_roster_matches_flush',
            },
            '_refresh_roster_matches_flush': {
                'join_tournament', 'admin_add_participant', 'leave_tournament',
                'admin_remove_participant', 'remove_participants_without_tickets',
            },
            '_dispatch_roster_invitations_after_signals': {
                'join_tournament', 'admin_add_participant', 'leave_tournament',
                'admin_remove_participant', 'remove_participants_without_tickets',
            },
        },
        'tournament_team_service.py': {
            '_refresh_roster_matches_flush': {
                'delete_team', 'join_team', 'leave_team', 'transfer_captain',
                'admin_add_member', 'remove_team_member',
            },
            '_dispatch_roster_invitations_after_signals': {
                'delete_team', 'join_team', 'leave_team', 'transfer_captain',
                'admin_add_member', 'remove_team_member',
            },
        },
        'tournament_seeding_service.py': {
            'tournament_match_service.collect_generation_invitations_flush': {'_generate_locked'},
        },
        'tournament_qualification_service.py': {
            'tournament_match_service.collect_generation_invitations_flush': {'_release_locked'},
        },
        'tournament_match_service.py': {
            'collect_generation_invitations_flush': {
                '_generate_single_elimination_impl', '_generate_double_elimination_impl',
                '_generate_round_robin_impl',
            },
            'tournament_readiness_service.refresh_pairing_and_invitations_flush': {
                '_create_match_contestant_flush',
            },
            'tournament_readiness_service.reset_readiness_flush': {'_reset_match_readiness_flush'},
        },
        'tournament_readiness_service.py': {
            'repository.clear_match_readiness_flush': {'_refresh_or_reset'},
            'repository.refresh_match_pairing_flush': {'_refresh_or_reset'},
        },
        'tournament_repository.py': {
            '_clear_match_readiness': {
                'clear_match_readiness_flush', 'refresh_match_pairing_flush',
                '_retire_match_pairing_flush',
            },
        },
    }
    for filename, adapters in manifest.items():
        functions = _functions(filename)
        for adapter, expected in adapters.items():
            actual = {name for name, node in functions.items() if adapter in _calls(node)}
            assert actual == expected, (filename, adapter, actual)
    roster = _functions('tournament_participant_service.py')['_refresh_roster_matches_flush']
    assert ast.unparse(roster.returns) == 'Result[tuple[MatchInvitationID, ...], str]'
    assert any(isinstance(node, ast.Attribute) and node.attr == 'pending_invitation_ids'
               for node in ast.walk(roster))
    assert 'reconcile_match_invitations_flush' not in {
        call.rsplit('.', 1)[-1] for call in _calls(roster)
    }
    assert 'tournament_repository.commit_session' not in _calls(roster)


def test_all_contestant_and_reset_writers_are_adapted():
    functions = _functions('tournament_match_service.py')
    raw_writers = {
        'tournament_repository.create_match_contestant': '_create_match_contestant_flush',
        'tournament_repository.delete_contestant_from_match': '_delete_contestant_from_match_flush',
        'tournament_repository.delete_contestants_for_match_flush': '_delete_contestants_for_match_flush',
        'tournament_repository.unconfirm_match': '_reset_match_readiness_flush',
    }
    for writer, adapter in raw_writers.items():
        owners = {name for name, node in functions.items() if writer in _calls(node)}
        assert owners == {adapter}, (writer, owners)
    forbidden = {
        'tournament_repository.delete_contestants_for_match',
        'tournament_repository.delete_match',
        'tournament_repository.delete_comments_for_match',
    }
    assert not any(_calls(node) & forbidden for node in functions.values())
    manifest = {
        '_create_match_contestant_flush': {
            '_generate_single_elimination_impl', '_generate_double_elimination_impl',
            '_generate_round_robin_impl', '_process_defwin_entries', '_advance_winner',
            '_advance_loser_to_lb', '_try_lb_defwin_advance', '_create_bracket_reset',
            '_generate_ffa_round_impl', 'generate_ffa_grand_final',
        },
        '_delete_contestant_from_match_flush': {
            'handle_defwin_for_removed_participant', 'handle_defwin_for_removed_team',
            '_unconfirm_match_impl',
        },
        '_delete_contestants_for_match_flush': {
            'clear_bracket', '_unconfirm_match_impl', 'delete_match', '_delete_matches_flush',
        },
        '_reset_match_readiness_flush': {'_unconfirm_match_impl'},
        'tournament_repository.delete_match_flush': {
            'clear_bracket', '_unconfirm_match_impl', 'delete_match', '_delete_matches_flush',
        },
    }
    for adapter, expected in manifest.items():
        assert {name for name, node in functions.items() if adapter in _calls(node)} == expected
    repository = _functions('tournament_repository.py')
    # Pin every low-level association INSERT/DELETE, including bulk writers.
    inserts = {name for name, node in repository.items()
               if 'DbTournamentMatchToContestant' in _calls(node)}
    assert inserts == {'create_match_contestant'}
    deletes = {
        name for name, node in repository.items()
        if any(isinstance(call, ast.Call) and ast.unparse(call.func) in {'delete', 'db.delete'}
               and call.args and ast.unparse(call.args[0]) == 'DbTournamentMatchToContestant'
               for call in ast.walk(node))
    }
    assert deletes == {
        'delete_match_contestant_flush', 'delete_contestant_from_match',
        'delete_contestants_for_match_flush', 'delete_contestants_for_tournament_flush',
        'remove_team_from_contestants_flush',
    }
    assert 'tournament_readiness_service.refresh_pairing_and_invitations_flush' in _calls(
        functions['_create_match_contestant_flush'])
    assert 'tournament_readiness_service.reset_readiness_flush' in _calls(
        functions['_reset_match_readiness_flush'])
    for adapter in raw_writers.values():
        assert 'tournament_repository.commit_session' not in _calls(functions[adapter])
    # All new audits explicitly opt out of the log service's default commit.
    for name in ('_audit_engine_pairing_change_flush', '_reset_match_readiness_flush'):
        audits = [call for call in ast.walk(functions[name])
                  if isinstance(call, ast.Call) and ast.unparse(call.func) == 'create_log_entry']
        assert audits
        assert all(any(keyword.arg == 'commit' and ast.literal_eval(keyword.value) is False
                       for keyword in call.keywords) for call in audits)
        assert 'tournament_repository.rollback_session' in _calls(functions[name])


def test_bulk_delete_has_flush_cleanup():
    functions = _functions('tournament_repository.py')
    for name in (
        'delete_match_flush', 'delete_matches_for_tournament',
        'delete_match_contestant_flush', 'delete_contestant_from_match',
        'delete_contestants_for_match_flush', 'delete_contestants_for_tournament_flush',
        'remove_team_from_contestants_flush',
    ):
        assert '_retire_match_pairing_flush' in _calls(functions[name]), name
    for wrapper, delegate in {
        'delete_match': 'delete_match_flush',
        'delete_match_contestant': 'delete_match_contestant_flush',
        'delete_contestants_for_match': 'delete_contestants_for_match_flush',
        'delete_contestants_for_tournament': 'delete_contestants_for_tournament_flush',
        'remove_team_from_contestants': 'remove_team_from_contestants_flush',
    }.items():
        assert delegate in _calls(functions[wrapper])


def test_engine_insert_owns_rollback_on_readiness_error():
    repository = Mock()
    contestant = SimpleNamespace(tournament_match_id='match')
    with patch.object(engine, 'tournament_repository', repository), patch.object(
        readiness, 'refresh_pairing_and_invitations_flush', return_value=Err('readiness_audit_failed'),
    ):
        with pytest.raises(ValueError, match='readiness_audit_failed'):
            engine._create_match_contestant_flush(contestant)
    repository.create_match_contestant.assert_called_once_with(contestant)
    repository.rollback_session.assert_called_once_with()
    repository.commit_session.assert_not_called()


def test_engine_insert_is_flush_only_and_has_no_signal_dispatch():
    repository = Mock()
    contestant = SimpleNamespace(tournament_match_id='match')
    with patch.object(engine, 'tournament_repository', repository), patch.object(
        readiness, 'refresh_pairing_and_invitations_flush', return_value=Ok(None),
    ), patch.object(readiness, 'dispatch_readiness_effects') as dispatch:
        engine._create_match_contestant_flush(contestant)
    repository.commit_session.assert_not_called()
    repository.rollback_session.assert_not_called()
    dispatch.assert_not_called()


# -- the final-source manifest: every writer path and every owner path --

APPLICATION = ROOT / 'byceps'
PACKAGE = 'services/lan_tournament/'
MATCH = PACKAGE + 'tournament_match_service.py'
REPOSITORY = PACKAGE + 'tournament_repository.py'
OPERATIONAL = PACKAGE + 'tournament_operational_service.py'
READINESS = PACKAGE + 'tournament_readiness_service.py'
INVITATION = PACKAGE + 'tournament_invitation_service.py'
HANDLERS = PACKAGE + 'notification_handlers.py'
LIFECYCLE = PACKAGE + 'tournament_service.py'
PARTICIPANT = PACKAGE + 'tournament_participant_service.py'
TEAM = PACKAGE + 'tournament_team_service.py'
SEEDING = PACKAGE + 'tournament_seeding_service.py'
QUALIFICATION = PACKAGE + 'tournament_qualification_service.py'
ORGA = PACKAGE + 'tournament_orga_service.py'
SITE_VIEWS = PACKAGE + 'blueprints/site/views.py'
ADMIN_VIEWS = PACKAGE + 'blueprints/admin/views.py'
REPOSITORY_RECEIVERS = {'tournament_repository', 'repository'}


@cache
def _application():
    """Parse every module which can reach the module under test, once."""
    modules = {}
    for path in sorted(APPLICATION.rglob('*.py')):
        relative = path.relative_to(APPLICATION).as_posix()
        text = path.read_text()
        if relative.startswith(PACKAGE) or 'lan_tournament' in text:
            modules[relative] = ast.parse(text)
    return modules


def _top_level_scopes(tree):
    """Yield (name, node) of each function, method and module statement."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f'{node.name}.{child.name}', child
        else:
            yield '<module>', node


def _call_sites(tree):
    for scope, node in _top_level_scopes(tree):
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                yield scope, ast.unparse(call.func), call.lineno


def _function(relative, name):
    return dict(_top_level_scopes(_application()[relative]))[name]


def _call_lines(node, name):
    """Source lines of the calls whose callee ends in `name`."""
    return sorted(
        call.lineno
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and ast.unparse(call.func).rpartition('.')[2] == name
    )


def _attribute_lines(node, name):
    return sorted(
        child.lineno
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute) and child.attr == name
    )


def _callers(name):
    """(module, function) of every call whose callee ends in `name`."""
    return {
        (relative, scope)
        for relative, tree in _application().items()
        for scope, func, _ in _call_sites(tree)
        if func.rpartition('.')[2] == name
    }


def _closure_calls(relative, start):
    """Every callee reachable from `start` through the module's own functions."""
    scopes = dict(_top_level_scopes(_application()[relative]))
    seen, calls, stack = set(), set(), [start]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        for call in ast.walk(scopes[name]):
            if isinstance(call, ast.Call):
                callee = ast.unparse(call.func)
                calls.add(callee)
                if callee in scopes:
                    stack.append(callee)
    return calls


# fmt: off
WRITER_CALLERS = {
    # Occupancy and bulk writers: each one has a transactional adapter.
    'create_match_contestant': {(MATCH, '_create_match_contestant_flush')},
    'delete_contestant_from_match': {(MATCH, '_delete_contestant_from_match_flush')},
    'delete_contestants_for_match_flush': {(MATCH, '_delete_contestants_for_match_flush')},
    'delete_contestants_for_tournament': {(LIFECYCLE, 'delete_tournament')},
    'delete_matches_for_tournament': {(LIFECYCLE, 'delete_tournament')},
    'remove_team_from_contestants_flush': {(TEAM, '_remove_team_contestants_flush')},
    'delete_match_flush': {
        (MATCH, 'clear_bracket'), (MATCH, '_unconfirm_match_impl'),
        (MATCH, 'delete_match'), (MATCH, '_delete_matches_flush')},
    # Public commit wrappers and unused legacy writers have no caller at all.
    'delete_contestants_for_match': set(),
    'delete_contestants_for_tournament_flush': set(),
    'delete_match': set(),
    'delete_match_contestant': set(),
    'delete_match_contestant_flush': set(),
    'remove_team_from_contestants': set(),
    'set_both_ready_notified_flush': set(),
    'mark_matches_both_ready_notified': set(),
    'set_occupied_since_if_unset_flush': set(),
    'set_ffa_lobby_occupied_since_if_unset_flush': {
        (OPERATIONAL, 'mark_completed_lobbies_occupied_flush')},
    # Reset and pairing writers.
    'unconfirm_match': {(MATCH, '_reset_match_readiness_flush')},
    'clear_match_readiness_flush': {
        (MATCH, '_reset_match_readiness_flush'), (READINESS, '_refresh_or_reset')},
    'refresh_match_pairing_flush': {(READINESS, '_refresh_or_reset')},
    'suppress_match_invitations_flush': {(LIFECYCLE, '_reconcile_lifecycle_invitations_flush')},
    # Claims, revocations and holds belong to the two Ready operations.
    'set_side_ready_flush': {(READINESS, 'claim_ready_flush')},
    'clear_side_ready_flush': {(READINESS, 'revoke_ready_flush')},
    'set_side_invitation_hold_flush': {
        (READINESS, 'claim_ready_flush'), (READINESS, 'revoke_ready_flush')},
    'set_readiness_revision_flush': {
        (READINESS, 'claim_ready_flush'), (READINESS, 'revoke_ready_flush')},
    # Durable recipient work belongs to the module-local service and handlers.
    'ensure_invitation_intents_flush': {(INVITATION, 'reconcile_match_invitations_flush')},
    'claim_invitation_dispatch_flush': {(INVITATION, '_dispatch_batch')},
    'record_invitation_outcome_flush': {(INVITATION, '_record_outcome')},
    'recover_expired_invitations_flush': {(INVITATION, '_sweep_once')},
    'select_invitation_retry_ids_flush': {(INVITATION, '_sweep_once')},
}
# fmt: on


def test_application_wide_low_level_writer_callers_are_exactly_the_adapters():
    names = set(WRITER_CALLERS)
    # Generic names may legitimately be endpoint or event strings elsewhere.
    distinctive = {
        name for name in names
        if name.endswith('_flush') or 'contestant' in name or 'invitation' in name
    }
    found = {name: set() for name in names}
    for relative, tree in _application().items():
        if relative == REPOSITORY:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.alias) and node.name == 'tournament_repository':
                assert node.asname in (None, 'repository'), (relative, node.asname)
            if isinstance(node, ast.ImportFrom) and (
                (node.module or '').endswith('tournament_repository')
            ):
                imported = {alias.name for alias in node.names} & names
                assert not imported, (relative, node.lineno, imported)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in distinctive, (relative, node.lineno)
        for scope, func, _ in _call_sites(tree):
            receiver, _, attribute = func.rpartition('.')
            if attribute in names and receiver in REPOSITORY_RECEIVERS:
                found[attribute].add((relative, scope))
    assert found == WRITER_CALLERS


READINESS_COLUMNS = {
    'ready_at_a', 'ready_at_b', 'ready_by_a', 'ready_by_b',
    'invitation_hold_a', 'invitation_hold_b',
    'pairing_generation', 'pairing_id', 'readiness_revision',
    'both_ready_notified_at', 'occupied_since',
}
REPOSITORY_COLUMN_WRITERS = {
    '_clear_match_readiness', 'clear_match_readiness_flush',
    'clear_side_ready_flush', 'refresh_match_pairing_flush',
    '_retire_match_pairing_flush', 'set_both_ready_notified_flush',
    'set_occupied_since_if_unset_flush', 'set_readiness_revision_flush',
    'set_side_invitation_hold_flush', 'set_side_ready_flush',
    # The legacy bulk marker update, kept inert and without any caller.
    'mark_matches_both_ready_notified',
}


def _column_writes(tree):
    for scope, node in _top_level_scopes(tree):
        for child in ast.walk(node):
            targets = []
            if isinstance(child, ast.Assign):
                targets = child.targets
            elif isinstance(child, (ast.AugAssign, ast.AnnAssign)):
                targets = [child.target]
            for target in targets:
                for leaf in ast.walk(target):
                    if isinstance(leaf, ast.Attribute) and leaf.attr in READINESS_COLUMNS:
                        yield scope
            if isinstance(child, ast.Call) and (
                ast.unparse(child.func).rpartition('.')[2] == 'values'
            ):
                if any(keyword.arg in READINESS_COLUMNS for keyword in child.keywords):
                    yield scope


def test_readiness_columns_are_written_only_by_the_pinned_repository_functions():
    found = set()
    for relative, tree in _application().items():
        if relative.startswith(PACKAGE + 'dbmodels/'):
            continue
        found |= {(relative, scope) for scope in _column_writes(tree)}
    assert found == {(REPOSITORY, name) for name in REPOSITORY_COLUMN_WRITERS}


# fmt: off
PROTECTED_TABLE_MODELS = (
    DbTournamentMatch, DbTournamentMatchToContestant, DbMatchPairing,
    DbMatchInvitation,
)
PROTECTED_CLASS_NAMES = {model.__name__ for model in PROTECTED_TABLE_MODELS}
DASHBOARD_MODELS = tuple(
    value
    for value in vars(dashboard_dbmodels).values()
    if isinstance(value, type) and hasattr(value, '__table__')
)
DASHBOARD_CLASS_NAMES = {model.__name__ for model in DASHBOARD_MODELS}
# The dashboard repository may read these tables. The gate does not need it
# to exist.
DASHBOARD_REPOSITORY = PACKAGE + 'tournament_dashboard_repository.py'
STATEMENT_WRITERS = {'insert', 'pg_insert', 'update', 'delete'}
SESSION_WRITERS = {
    'add', 'add_all', 'merge', 'delete', 'bulk_save_objects',
    'bulk_insert_mappings', 'bulk_update_mappings',
}
# fmt: on


def _column_names(models):
    return {name for model in models for name in model.__table__.c.keys()}


# A column name the dashboard tables share (`id`, `tournament_id`, ...) says
# nothing about the table, so only the other names identify a protected write.
PROTECTED_COLUMNS = _column_names(PROTECTED_TABLE_MODELS) - _column_names(
    DASHBOARD_MODELS
)


def _table_referrers(trees):
    return {
        relative
        for relative, tree in trees.items()
        if not relative.startswith(PACKAGE + 'dbmodels/')
        and any(True for _ in _references(tree, PROTECTED_CLASS_NAMES))
    }


def test_only_the_repository_touches_the_pairing_work_and_occupancy_tables():
    # The dashboard repository may read them; the next tests pin that it
    # only reads. Any other referrer, existing or new, still fails here.
    referrers = _table_referrers(_application())
    assert referrers - {DASHBOARD_REPOSITORY} == {REPOSITORY}


# fmt: off
@pytest.mark.parametrize('extra', [
    PACKAGE + 'tournament_operational_service.py',
    PACKAGE + 'tournament_dashboard_service.py',
    PACKAGE + 'blueprints/admin/dashboard_views.py',
    'services/tourney/some_module.py',
])
@pytest.mark.parametrize('source', [
    'from .dbmodels.match import DbTournamentMatch',
    'from .dbmodels import match_readiness as m\nx = m.DbMatchPairing',
    'select(DbMatchInvitation.id)',
    'session.get(DbTournamentMatchToContestant, 1)',
])
# fmt: on
def test_a_new_referrer_of_the_protected_tables_is_detected(extra, source):
    trees = {**_application(), extra: ast.parse(source)}
    assert _table_referrers(trees) - {DASHBOARD_REPOSITORY} == {REPOSITORY, extra}


def test_the_dashboard_allowance_does_not_cover_a_lookalike_path():
    lookalike = PACKAGE + 'blueprints/tournament_dashboard_repository.py'
    trees = {**_application(), lookalike: ast.parse('DbTournamentMatch')}
    assert lookalike in _table_referrers(trees) - {DASHBOARD_REPOSITORY}


def test_the_dashboard_repository_allowance_holds_either_way():
    present = {
        **_application(), DASHBOARD_REPOSITORY: ast.parse('DbTournamentMatch'),
    }
    absent = {
        relative: tree
        for relative, tree in _application().items()
        if relative != DASHBOARD_REPOSITORY
    }
    for trees in (present, absent):
        assert _table_referrers(trees) - {DASHBOARD_REPOSITORY} == {REPOSITORY}


def _leaf(func):
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')


def _names_in(node):
    for leaf in ast.walk(node):
        if isinstance(leaf, ast.Name):
            yield leaf.id
        elif isinstance(leaf, ast.Attribute):
            yield leaf.attr


def _bindings(scope):
    """Yield (bound names, value) for each assignment-like binding."""
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            targets, value = [node.target], node.value
        elif isinstance(node, (ast.For, ast.comprehension)):
            targets, value = [node.target], node.iter
        elif isinstance(node, ast.withitem) and node.optional_vars:
            targets, value = [node.optional_vars], node.context_expr
        else:
            continue
        if value is not None:
            yield {
                leaf.id
                for target in targets
                for leaf in ast.walk(target)
                if isinstance(leaf, ast.Name) and isinstance(leaf.ctx, ast.Store)
            }, value


def _protected_rows(scope, classes):
    """Return names bound to values built from the protected classes alone.

    A value that also mentions a dashboard class is left out: a dashboard row
    loaded through a join with a match is the dashboard's own to change.
    """
    rows, grew = set(), True
    while grew:
        grew = False
        for names, value in _bindings(scope):
            used = set(_names_in(value))
            if (
                used & (classes | rows)
                and not used & DASHBOARD_CLASS_NAMES
                and not names <= rows
            ):
                rows |= names
                grew = True
    return rows


def _table_writes(tree):
    """Return (line, kind) of each write to a protected table, by syntax.

    It sees statements on the classes, instances of them, session writes of
    them and assignments to their own columns. A row reached through some
    other path under a shared column name (`id`, `tournament_id`) is beyond it.
    """
    found = []
    if tree is None:
        return found

    classes = PROTECTED_CLASS_NAMES | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in PROTECTED_CLASS_NAMES and alias.asname
    }
    for _, scope in _top_level_scopes(tree):
        rows = _protected_rows(scope, classes)
        mine = classes | rows
        for node in ast.walk(scope):
            if isinstance(node, ast.Call):
                leaf = _leaf(node.func)
                arguments = {
                    name
                    for argument in (*node.args, *(k.value for k in node.keywords))
                    for name in _names_in(argument)
                }
                on_session = (
                    ast.unparse(node.func).rpartition('.')[0].endswith('session')
                )
                if leaf in classes:
                    found.append((node.lineno, 'instance'))
                if leaf in STATEMENT_WRITERS and not on_session and (
                    arguments & mine
                ):
                    found.append((node.lineno, 'statement'))
                if leaf in SESSION_WRITERS and on_session and arguments & mine:
                    found.append((node.lineno, 'session'))
                if leaf == 'values' and any(
                    keyword.arg in PROTECTED_COLUMNS for keyword in node.keywords
                ):
                    found.append((node.lineno, 'values'))
                if leaf == 'setattr' and len(node.args) > 1 and (
                    set(_names_in(node.args[0])) & rows
                    or ast.unparse(node.args[1]).strip('\'"') in PROTECTED_COLUMNS
                ):
                    found.append((node.lineno, 'setattr'))
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                for leaf in ast.walk(target):
                    if isinstance(leaf, ast.Attribute) and (
                        leaf.attr in PROTECTED_COLUMNS
                        or set(_names_in(leaf.value)) & rows
                    ):
                        found.append((node.lineno, 'attribute'))
    return sorted(set(found))


def _in_function(source):
    body = ''.join(f'    {line}\n' for line in source.splitlines())
    return ast.parse(f'def work():\n{body}')


def test_the_dashboard_repository_only_reads_the_protected_tables():
    # Writes to the dashboard tables are its business; these four are not.
    assert _table_writes(_application().get(DASHBOARD_REPOSITORY)) == []


# fmt: off
@pytest.mark.parametrize('source, kinds', [
    ('update(DbTournamentMatch).values(round=1)', {'statement', 'values'}),
    ('db.update(DbMatchPairing)', {'statement'}),
    ('pg_insert(DbMatchInvitation).values(x=1)', {'statement'}),
    ('delete(DbTournamentMatchToContestant)', {'statement'}),
    ('db.session.add(DbTournamentMatch(id=1))', {'instance', 'session'}),
    ('row = DbMatchPairing(id=1)', {'instance'}),
    ('session.add_all([DbMatchInvitation(id=1)])', {'instance', 'session'}),
    ('x = update(anything).values(occupied_since=None)', {'values'}),
    ('m = db.session.get(DbTournamentMatch, 1)\nm.occupied_since = None', {'attribute'}),
    ('m = db.session.get(DbTournamentMatch, 1)\nm.tournament_id = 2', {'attribute'}),
    ('m = db.session.get(DbTournamentMatch, 1)\nm.round += 1', {'attribute'}),
    (
        (
            'rows = db.session.scalars(select(DbMatchPairing))\n'
            'for r in rows:\n    r.ended_at = None'
        ),
        {'attribute'},
    ),
    (
        (
            'stmt = select(DbTournamentMatch)\n'
            'rows = db.session.scalars(stmt)\n'
            'for r in rows:\n    db.session.delete(r)'
        ),
        {'session'},
    ),
    ('m = db.session.get(DbTournamentMatch, 1)\nsetattr(m, "x", 1)', {'setattr'}),
    ('setattr(anything, "ready_at_a", None)', {'setattr'}),
    ('anything.invitation_hold_a = True', {'attribute'}),
])
# fmt: on
def test_protected_table_writes_are_detected(source, kinds):
    assert {kind for _, kind in _table_writes(_in_function(source))} == kinds


# fmt: off
@pytest.mark.parametrize('source', [
    (
        'select(DbTournamentMatch.id, DbTournamentMatch.phase)'
        '.join(DbTournament, DbTournament.id == DbTournamentMatch.tournament_id)'
    ),
    'select(func.count()).select_from(DbTournamentMatchToContestant)',
    (
        'update(DbMatchDueEpisode).where(DbMatchDueEpisode.match_id.in_('
        'select(DbTournamentMatch.id))).values(closed_at=now)'
    ),
    'delete(DbMatchDashboardAnnotation).where(DbMatchDashboardAnnotation.match_id == 1)',
    'ep = DbMatchDueEpisode(id=1)\ndb.session.add(ep)',
    'ep = db.session.get(DbMatchDueEpisode, 1)\nep.ack_revision = 3\nep.tournament_id = 1',
    (
        'stmt = select(DbMatchDueEpisode).join(DbTournamentMatch, '
        'DbTournamentMatch.id == DbMatchDueEpisode.match_id)\n'
        'for ep in db.session.scalars(stmt):\n    ep.closed_at = now'
    ),
    'seen = set()\nseen.add(DbTournamentMatch)',
    'ids = db.session.scalars(select(DbTournamentMatch.id)).all()\nprint(ids)',
])
# fmt: on
def test_dashboard_table_writes_and_protected_reads_are_not_flagged(source):
    assert _table_writes(_in_function(source)) == []


# fmt: off
@pytest.mark.parametrize('source, kinds', [
    (
        (
            'from x import DbTournamentMatch as Match\n'
            'def f():\n    update(Match)'
        ),
        {'statement'},
    ),
    (
        (
            'from x import DbMatchPairing as Pair\n'
            'def f():\n    db.session.add(Pair(id=1))'
        ),
        {'instance', 'session'},
    ),
    (
        (
            'from x import DbTournamentMatch as Match\n'
            'def f():\n    m = db.session.get(Match, 1)\n'
            '    m.tournament_id = 2'
        ),
        {'attribute'},
    ),
    ('def f():\n    Match = DbTournamentMatch\n    update(Match)', {'statement'}),
])
# fmt: on
def test_an_aliased_protected_class_is_still_seen(source, kinds):
    assert {kind for _, kind in _table_writes(ast.parse(source))} == kinds


def test_only_distinctive_columns_identify_a_protected_write():
    assert {'occupied_since', 'ready_at_a', 'pairing_id', 'phase'} <= PROTECTED_COLUMNS
    assert not {'id', 'tournament_id', 'match_id'} & PROTECTED_COLUMNS


ROSTER_WRITERS = {
    'create_participant', 'reactivate_participant',
    'delete_participants_by_ids', 'soft_delete_participants_by_ids',
    'remove_team_from_participants_flush', 'soft_delete_team_flush',
    'update_participant_flush', 'update_team_captain_flush',
    'delete_team_flush', 'create_team', 'update_team',
}
ROSTER_OWNERS = {
    PARTICIPANT: {
        'join_tournament', 'admin_add_participant', 'leave_tournament',
        'admin_remove_participant', 'remove_participants_without_tickets',
    },
    TEAM: {
        'delete_team', 'join_team', 'leave_team', 'transfer_captain',
        'admin_add_member', 'remove_team_member',
    },
}
# A new team has no assignment, and a rename keeps every roster identity.
ROSTER_EXEMPT = {TEAM: {'create_team', 'update_team'}}


def test_every_roster_and_removal_writer_path_is_an_owner_or_an_exemption():
    for relative, owners in ROSTER_OWNERS.items():
        scopes = dict(_top_level_scopes(_application()[relative]))
        public = [
            name for name in scopes
            if not name.startswith(('_', '<')) and '.' not in name
        ]
        reaching = {
            name for name in public
            if any(
                callee.rpartition('.')[2] in ROSTER_WRITERS
                and callee.rpartition('.')[0] in REPOSITORY_RECEIVERS
                for callee in _closure_calls(relative, name)
            )
        }
        assert reaching == owners | ROSTER_EXEMPT.get(relative, set()), relative
        for name in owners:
            calls = _closure_calls(relative, name)
            assert '_refresh_roster_matches_flush' in calls, (relative, name)
            assert '_dispatch_roster_invitations_after_signals' in calls, (
                relative, name,
            )


# fmt: off
OWNER_PATHS = [
    # (module, owner, where the durable intent is collected, post-commit effect)
    (MATCH, 'generate_single_elimination_bracket', '_generate_single_elimination_impl', 'dispatch_generation_events'),
    (MATCH, 'generate_double_elimination_bracket', '_generate_double_elimination_impl', 'dispatch_generation_events'),
    (MATCH, 'generate_round_robin_bracket', '_generate_round_robin_impl', 'dispatch_generation_events'),
    (MATCH, 'admin_set_and_confirm_match', '_pending_invitations_flush', '_dispatch_confirmation_effects'),
    (MATCH, 'confirm_match', '_pending_invitations_flush', '_dispatch_confirmation_effects'),
    (MATCH, 'unconfirm_match', '_pending_invitations_flush', 'dispatch_pending_invitations'),
    (MATCH, 'correct_match_result', '_pending_invitations_flush', 'dispatch_pending_invitations'),
    (SEEDING, 'generate_from_seeding', '_generate_locked', 'dispatch_generation_events'),
    (QUALIFICATION, 'try_auto_release', '_auto_release_locked', 'dispatch_generation_events'),
    (LIFECYCLE, 'change_status', '_persist_status_change_flush', 'dispatch_pending_invitations'),
    *(
        (PARTICIPANT, name, '_refresh_roster_matches_flush', '_dispatch_roster_invitations_after_signals')
        for name in sorted(ROSTER_OWNERS[PARTICIPANT])
    ),
    *(
        (TEAM, name, '_refresh_roster_matches_flush', '_dispatch_roster_invitations_after_signals')
        for name in sorted(ROSTER_OWNERS[TEAM])
    ),
]
# fmt: on


@pytest.mark.parametrize(
    'relative,owner,intent,effect',
    OWNER_PATHS,
    ids=[f'{owner}' for _, owner, _, _ in OWNER_PATHS],
)
def test_durable_intents_precede_the_owning_commit_and_effects_follow_it(
    relative, owner, intent, effect
):
    function = _function(relative, owner)
    intents = _call_lines(function, intent)
    commits = _call_lines(function, 'commit_session')
    effects = _call_lines(function, effect)
    assert intents and effects, (relative, owner)
    assert len(commits) == 1, (relative, owner, commits)
    assert max(intents) < commits[0] < min(effects), (relative, owner)


def test_release_unrelease_and_seeding_owners_keep_the_same_order():
    release = _function(QUALIFICATION, 'release_playoffs')
    assert (
        _call_lines(release, '_release_locked')[0]
        < _call_lines(release, '_finish_release')[0]
    )
    finish = _function(QUALIFICATION, '_finish_release')
    assert (
        _call_lines(finish, 'commit_session')[0]
        < _call_lines(finish, 'dispatch_generation_events')[0]
    )
    assert _call_lines(_function(QUALIFICATION, '_auto_release_locked'), '_release_locked')
    unrelease = _function(QUALIFICATION, 'unrelease_playoffs')
    assert (
        _call_lines(unrelease, '_unrelease_locked')[0]
        < _call_lines(unrelease, 'commit_session')[0]
    )
    # Taking the release back closes the phase-two history before the commit.
    assert _call_lines(_function(QUALIFICATION, '_unrelease_locked'), 'clear_bracket')
    released = _function(QUALIFICATION, '_release_locked')
    assert _call_lines(released, 'stage_generation')
    assert _call_lines(released, 'collect_generation_invitations_flush')
    staged = _function(SEEDING, 'stage_generation')
    assert _call_lines(staged, '_generate_locked')
    assert _call_lines(_function(SEEDING, '_generate_locked'), 'collect_generation_invitations_flush')


# fmt: off
COLLECTORS = {
    'collect_generation_invitations_flush': {
        (MATCH, '_generate_single_elimination_impl'),
        (MATCH, '_generate_double_elimination_impl'),
        (MATCH, '_generate_round_robin_impl'),
        (SEEDING, '_generate_locked'),
        (QUALIFICATION, '_release_locked')},
    '_pending_invitations_flush': {
        (MATCH, '_reset_match_readiness_flush'),
        (MATCH, 'collect_generation_invitations_flush'),
        (MATCH, 'admin_set_and_confirm_match'), (MATCH, 'confirm_match'),
        (MATCH, 'unconfirm_match'), (MATCH, 'correct_match_result')},
    'reconcile_invitations_flush': {
        (MATCH, '_pending_invitations_flush'),
        (READINESS, 'claim_ready_flush'), (READINESS, 'revoke_ready_flush'),
        (READINESS, '_refresh_or_reset')},
    'reconcile_match_invitations_flush': {
        (READINESS, 'reconcile_invitations_flush'),
        (LIFECYCLE, '_reconcile_lifecycle_invitations_flush')},
    'dispatch_pending_invitations': {
        (MATCH, 'dispatch_generation_events'), (MATCH, '_dispatch_confirmation_effects'),
        (MATCH, 'unconfirm_match'), (MATCH, 'correct_match_result'),
        (PARTICIPANT, '_dispatch_roster_invitations_after_signals'),
        (READINESS, 'dispatch_readiness_effects'), (LIFECYCLE, 'change_status')},
    # Post-commit work is one queue job per batch; the job body is only ever
    # referenced as the job function, never called.
    'enqueue_invitation_dispatch': {(READINESS, 'dispatch_pending_invitations')},
    'enqueue_tournament_sweep': {(HANDLERS, '_on_tournament_status_changed')},
}
# fmt: on


@pytest.mark.parametrize('name', sorted(COLLECTORS))
def test_intent_collection_and_dispatch_callers_are_the_pinned_owner_paths(name):
    assert _callers(name) == COLLECTORS[name]


def test_signal_handlers_are_not_the_only_intent_creators():
    handlers = _application()[HANDLERS]
    called = {func.rpartition('.')[2] for _, func, _ in _call_sites(handlers)}
    # The handlers only catch up from the persisted assignment; the owner
    # side collectors never run inside a signal handler.
    assert 'reconcile_invitations_flush' not in called
    assert 'collect_generation_invitations_flush' not in called
    assert not list(
        _references(
            handlers,
            {'match_both_ready', 'match_ready_claimed', 'match_ready_revoked'},
        )
    )
    connected = {
        func for _, func, _ in _call_sites(handlers) if func.endswith('.connect')
    }
    assert connected == {
        'match_ready.connect', 'tournament_status_changed.connect',
        'tournament_request_accepted.connect',
        'tournament_request_rejected.connect',
    }
    # The readiness operations create their intents in their own transaction.
    for name in ('claim_ready_flush', 'revoke_ready_flush', '_refresh_or_reset'):
        assert _call_lines(_function(READINESS, name), 'reconcile_invitations_flush'), name


def test_every_generation_outcome_is_collected_by_its_owner():
    assert _callers('GenerationOutcome') == {
        (MATCH, '_generate_single_elimination_impl'),
        (MATCH, '_generate_double_elimination_impl'),
        (MATCH, '_generate_round_robin_impl'),
        (MATCH, '_generate_ffa_initial_impl'),
        (MATCH, '_generate_ffa_advance_impl'),
        (MATCH, 'generate_ffa_grand_final'),
        (SEEDING, '_generate_locked'),
    }
    for name in (
        '_generate_single_elimination_impl', '_generate_double_elimination_impl',
        '_generate_round_robin_impl',
    ):
        function = _function(MATCH, name)
        assert _call_lines(function, 'collect_generation_invitations_flush'), name
    # The FFA generators are only reachable through the seeding owner, which
    # collects the final coalesced outcome before its commit.
    assert _callers('_generate_ffa_initial_impl') == {(SEEDING, '_run_generator')}
    assert _callers('_generate_ffa_advance_impl') == {(SEEDING, '_run_generator')}
    assert _callers('_run_generator') == {(SEEDING, '_generate_locked')}
    # The two outcomes `_generate_locked` builds itself generated nothing.
    empty = [
        call for call in ast.walk(_function(SEEDING, '_generate_locked'))
        if isinstance(call, ast.Call)
        and ast.unparse(call.func).rpartition('.')[2] == 'GenerationOutcome'
    ]
    assert len(empty) == 2
    for call in empty:
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in call.keywords}
        assert keywords['ready_match_ids'] == 'frozenset()'
    # FFA matches carry no readiness pairing and no assignment invitation, so
    # the grand final collects nothing. Changing that must change this pin.
    grand_final = _function(MATCH, 'generate_ffa_grand_final')
    assert not _call_lines(grand_final, 'collect_generation_invitations_flush')
    assert (
        _call_lines(grand_final, 'commit_session')[0]
        < _call_lines(grand_final, 'dispatch_generation_events')[0]
    )


def test_tournament_deletion_closes_history_before_rows_and_commits_once():
    function = _function(LIFECYCLE, 'delete_tournament')
    names = [
        'lock_tournament_for_update', 'delete_submissions_for_tournament',
        'delete_comments_for_tournament', 'delete_contestants_for_tournament',
        'delete_matches_for_tournament', 'delete_participants_for_tournament',
        'delete_teams_for_tournament', 'commit_session',
    ]
    lines = [_call_lines(function, name) for name in names]
    assert all(lines), dict(zip(names, lines, strict=True))
    assert [found[0] for found in lines] == sorted(found[0] for found in lines)
    assert len(lines[-1]) == 1
    # The bulk writers close the pairing history and suppress unsent work
    # inside this one transaction: none of them may commit on its own.
    for call in ast.walk(function):
        if isinstance(call, ast.Call) and ast.unparse(call.func).rpartition('.')[2] in {
            'delete_submissions_for_tournament', 'delete_comments_for_tournament',
            'delete_contestants_for_tournament', 'delete_matches_for_tournament',
            'delete_participants_for_tournament', 'delete_teams_for_tournament',
        }:
            assert any(
                keyword.arg == 'commit' and ast.literal_eval(keyword.value) is False
                for keyword in call.keywords
            ), ast.unparse(call.func)


def test_scope_dependent_writers_lock_the_tournament_before_reading_scope():
    revoke = _function(ORGA, 'revoke_orga')
    assert (
        _call_lines(revoke, 'lock_tournament_for_update')[0]
        < _call_lines(revoke, 'find_orga_for_tournament_and_user')[0]
        < _call_lines(revoke, 'delete_orga')[0]
        < _call_lines(revoke, 'commit')[0]
    )
    locked = _function(READINESS, '_locked_facts')
    assert (
        _call_lines(locked, 'get_tournament_for_update')[0]
        < _call_lines(locked, 'get_match_for_update')[0]
    )
    validate = _function(READINESS, '_validate')
    assert (
        _call_lines(validate, '_locked_facts')[0]
        < _call_lines(validate, '_fresh_authority_inputs')[0]
        < _call_lines(validate, 'authorize_readiness_side')[0]
    )


def test_catchup_locks_the_tournament_before_recovering_and_selecting_work():
    sweep = _function(INVITATION, '_sweep_once')
    lock = _call_lines(sweep, 'lock_tournament_for_update')[0]
    recover = _call_lines(sweep, 'recover_expired_invitations_flush')[0]
    select = _call_lines(sweep, 'select_invitation_retry_ids_flush')[0]
    commit = _call_lines(sweep, 'commit_session')[0]
    assert lock < recover < select < commit
    # The handler only queues the sweep; it holds no lock or storage call.
    catchup = _function(HANDLERS, '_on_tournament_status_changed')
    assert _call_lines(catchup, 'enqueue_tournament_sweep')
    for name in (
        'lock_tournament_for_update', 'recover_expired_invitations_flush',
        'select_invitation_retry_ids_flush', 'commit_session',
        'reconcile_match_invitations_flush',
    ):
        assert not _call_lines(catchup, name), name


# fmt: off
POST_ROUTES = [
    # (module, route, mutation operations, post-commit effect)
    (SITE_VIEWS, '_mutate_readiness', ('claim_ready_flush', 'revoke_ready_flush'), 'dispatch_readiness_effects'),
]
ROUTE_DECORATORS = {
    (SITE_VIEWS, 'claim_ready'): [
        "blueprint.post('/matches/<match_id>/ready/claim')", 'login_required'],
    (SITE_VIEWS, 'revoke_ready'): [
        "blueprint.post('/matches/<match_id>/ready/revoke')", 'login_required'],
}
# fmt: on


@pytest.mark.parametrize(
    'relative,route,operations,effect',
    POST_ROUTES,
    ids=[route for _, route, _, _ in POST_ROUTES],
)
def test_native_post_routes_check_the_token_first_and_commit_once_before_effects(
    relative, route, operations, effect
):
    function = _function(relative, route)
    tokens = _call_lines(function, 'validate_readiness_csrf_token')
    lookups = _call_lines(function, '_get_tournament_or_404')
    mutations = sorted(
        line for operation in operations
        for line in _attribute_lines(function, operation)
    )
    commits = _call_lines(function, 'commit_session')
    effects = _call_lines(function, effect)
    where = (relative, route)
    assert len(tokens) == 1 and lookups and mutations, where
    assert len(commits) == 1 and effects, where
    # Token, then subject lookup, then the one operation, one commit, effects.
    assert tokens[0] < lookups[0] <= mutations[0] < commits[0] < effects[0], where
    # Even a rejected token is refused before any database read of the subject.
    for lookup in ('get_match', 'find_match', 'get_tournament', 'find_tournament'):
        assert all(tokens[0] < line for line in _call_lines(function, lookup)), (where, lookup)


def test_native_post_routes_are_post_only_and_authenticated():
    for (relative, route), expected in ROUTE_DECORATORS.items():
        decorators = [
            ast.unparse(decorator)
            for decorator in _function(relative, route).decorator_list
        ]
        assert decorators == expected, (relative, route)


def test_no_other_view_mutates_readiness_or_queues_invitations():
    mutations = {
        'claim_ready_flush', 'revoke_ready_flush',
        'refresh_pairing_and_invitations_flush', 'reset_readiness_flush',
        'dispatch_readiness_effects', 'dispatch_pending_invitations',
    }
    found = set()
    for relative, tree in _application().items():
        if not relative.startswith(PACKAGE + 'blueprints/'):
            continue
        for scope, node in _top_level_scopes(tree):
            if any(
                isinstance(child, ast.Attribute) and child.attr in mutations
                for child in ast.walk(node)
            ):
                found.add((relative, scope))
    assert found == {
        (SITE_VIEWS, '_mutate_readiness'),
    }
