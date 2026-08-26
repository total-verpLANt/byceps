"""Why the signal handlers can lean on the writers that emit their signals."""

import ast
from pathlib import Path

import pytest

import byceps.services.lan_tournament as package


ROOT = Path(package.__file__).parent


def _functions(path: Path):
    tree = ast.parse(path.read_text())
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }


def _call_names(node) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def _send_sites(signal: str) -> set[tuple[str, str]]:
    sites = set()
    for path in sorted(ROOT.rglob('*.py')):
        if 'tests' in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for function in (
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        ):
            for call in (
                n for n in ast.walk(function) if isinstance(n, ast.Call)
            ):
                func = call.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == 'send'
                    and getattr(
                        func.value, 'id', getattr(func.value, 'attr', None)
                    )
                    == signal
                ):
                    sites.add((path.name, function.name))
    return sites


def test_match_ready_has_exactly_the_audited_emitters():
    # A new emitter must be audited before the handler can stay passive.
    assert _send_sites('match_ready') == {
        ('tournament_match_service.py', 'dispatch_generation_events'),
        ('tournament_match_service.py', '_dispatch_confirmation_effects'),
        ('tournament_match_service.py', 'correct_match_result'),
    }


def test_match_ready_emitting_owners_reconcile_and_dispatch_their_ids():
    functions = _functions(ROOT / 'tournament_match_service.py')
    # confirm and result correction reconcile the ready matches before commit.
    for name in (
        'admin_set_and_confirm_match',
        'confirm_match',
        'correct_match_result',
    ):
        assert '_pending_invitations_flush' in _call_names(functions[name]), (
            name
        )
    assert 'dispatch_pending_invitations' in _call_names(
        functions['_dispatch_confirmation_effects']
    )
    assert 'dispatch_pending_invitations' in _call_names(
        functions['correct_match_result']
    )
    assert 'dispatch_pending_invitations' in _call_names(
        functions['dispatch_generation_events']
    )
    # generation reconciles the ready matches in the generator transaction.
    assert 'reconcile_invitations_flush' in _call_names(
        functions['_pending_invitations_flush']
    )
    assert 'collect_generation_invitations_flush' in _call_names(
        functions['_generate_single_elimination_impl']
    )
    assert 'collect_generation_invitations_flush' in _call_names(
        functions['_generate_double_elimination_impl']
    )
    assert 'collect_generation_invitations_flush' in _call_names(
        functions['_generate_round_robin_impl']
    )


def test_ffa_grand_final_is_the_one_emitter_that_does_not_reconcile():
    # Its matches have no one-versus-one audience, so `_on_match_ready` has
    # nothing to catch up. A second emitter without a reconcile would lose
    # invitations silently: give it one, or audit it here.
    functions = _functions(ROOT / 'tournament_match_service.py')
    called = _call_names(functions['generate_ffa_grand_final'])
    assert 'dispatch_generation_events' in called
    assert 'collect_generation_invitations_flush' not in called
    assert '_pending_invitations_flush' not in called


def test_generation_dispatch_has_exactly_the_audited_callers():
    callers = set()
    for path in sorted(ROOT.rglob('*.py')):
        if 'tests' in path.parts:
            continue
        for name, function in _functions(path).items():
            if 'dispatch_generation_events' in _call_names(function):
                callers.add((path.name, name))
    # Each one hands over an outcome built by a generator that went through
    # `collect_generation_invitations_flush`, except the FFA grand final.
    assert callers == {
        ('tournament_match_service.py', 'generate_single_elimination_bracket'),
        ('tournament_match_service.py', 'generate_double_elimination_bracket'),
        ('tournament_match_service.py', 'generate_round_robin_bracket'),
        ('tournament_match_service.py', 'generate_ffa_grand_final'),
        ('tournament_seeding_service.py', 'generate_from_seeding'),
        ('tournament_qualification_service.py', 'try_auto_release'),
        ('tournament_qualification_service.py', '_finish_release'),
    }
    for module, collector in (
        ('tournament_seeding_service.py', '_generate_locked'),
        ('tournament_qualification_service.py', '_release_locked'),
    ):
        assert 'collect_generation_invitations_flush' in _call_names(
            _functions(ROOT / module)[collector]
        ), (module, collector)


def test_status_change_owner_reconciles_in_its_transaction_and_dispatches_after_commit():
    functions = _functions(ROOT / 'tournament_service.py')
    assert '_reconcile_lifecycle_invitations_flush' in _call_names(
        functions['_persist_status_change_flush']
    )
    change = functions['change_status']
    names = [
        call.func.attr
        if isinstance(call.func, ast.Attribute)
        else getattr(call.func, 'id', '')
        for call in ast.walk(change)
        if isinstance(call, ast.Call)
    ]
    assert 'dispatch_pending_invitations' in names
    assert 'commit_session' in names
    lines = {
        name: min(
            call.lineno
            for call in ast.walk(change)
            if isinstance(call, ast.Call)
            and (
                getattr(call.func, 'attr', None)
                or getattr(call.func, 'id', None)
            )
            == name
        )
        for name in (
            '_persist_status_change_flush',
            'commit_session',
            '_dispatch_status_change_effects',
            'dispatch_pending_invitations',
        )
    }
    assert (
        lines['_persist_status_change_flush']
        < lines['commit_session']
        < lines['_dispatch_status_change_effects']
        <= lines['dispatch_pending_invitations']
    )


def test_status_changed_has_the_single_post_commit_emitter():
    assert _send_sites('tournament_status_changed') == {
        ('tournament_service.py', '_dispatch_status_change_effects'),
    }


@pytest.mark.parametrize(
    'handler', ['_on_match_ready', '_on_tournament_status_changed']
)
def test_handlers_stay_registered_for_the_core_wiring(handler):
    from byceps.services.lan_tournament import notification_handlers

    assert callable(getattr(notification_handlers, handler))
    assert callable(notification_handlers.enable_match_notifications)
