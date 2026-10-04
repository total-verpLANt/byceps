import argparse
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
import difflib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import NamedTuple


REPO_ROOT = Path(__file__).resolve().parents[4]

LEGACY_CHAIR_PREFIX = 'byceps/services/chair_optout/'
CHAIR_PREFIX = 'byceps/services/chair_planning/'
TRANSLATIONS_PREFIX = 'byceps/translations/'
STATIC_PREFIX = 'byceps/static/'
MORE_PREFIX = 'byceps/services/more/'
TICKET_TEMPLATE = (
    'byceps/services/ticketing/blueprints/site/templates'
    '/site/ticketing/index_mine.html'
)
BUILD_FILES = frozenset(
    {'Dockerfile', 'justfile', '.dockerignore', 'pyproject.toml'}
)
EXEMPT_PREFIXES = (
    LEGACY_CHAIR_PREFIX,
    CHAIR_PREFIX,
    TRANSLATIONS_PREFIX,
    'tests/',
    'sites/',
)
EXEMPT_PATHS = frozenset({'uv.lock'})
METADATA_FILES = frozenset({'.gitattributes', '.gitmodules'})
PATH_DIFF_FLAGS = ('--no-renames', '--ignore-submodules=none')
REGULAR_MODES = frozenset({'100644', '100755'})
# History before the rename carries the legacy loader lines. A state must use
# one complete set; the sets are never mixed.
LEGACY_LOADER_LINES = {
    'byceps/application.py': (
        'from byceps.services.chair_optout.lifecycle import enable_chair_lifecycle',
        '    enable_chair_lifecycle()',
        '',
    ),
    'byceps/blueprints/admin.py': (
        "        ('services.chair_optout.blueprints.admin', '/chair_optout'),",
    ),
    'byceps/blueprints/site.py': (
        "        ('services.chair_optout.blueprints.site', '/chair_optout'),",
    ),
}
LOADER_LINES = {
    'byceps/application.py': (
        'from byceps.services.chair_planning.lifecycle import enable_chair_lifecycle',
        '    enable_chair_lifecycle()',
        '',
    ),
    'byceps/blueprints/admin.py': (
        "        ('services.chair_planning.blueprints.admin', '/chair_planning'),",
    ),
    'byceps/blueprints/site.py': (
        "        ('services.chair_planning.blueprints.site', '/chair_planning'),",
    ),
}
# The current set comes first: it wins a tie when a state fits neither.
LOADER_LINE_SETS = {'current': LOADER_LINES, 'legacy': LEGACY_LOADER_LINES}

LEGACY_README_PATH = 'byceps/services/chair_optout/README.md'
README_PATH = 'byceps/services/chair_planning/README.md'
README_PATHS = (LEGACY_README_PATH, README_PATH)
README_COMPILE_PATTERN = re.compile(r'just\s+babel-compile|pybabel\s+compile')
BUILD_COMPILE_PATHS = (
    'Dockerfile',
    'justfile',
    'compose.yaml',
    'docker',
    'babel.cfg',
)
BUILD_COMPILE_PATTERN = re.compile(
    r'babel|pybabel|msgfmt|compile_catalog', re.IGNORECASE
)


class GitError(Exception):
    """Signal that a git call failed."""


class State(NamedTuple):
    """One tree to judge against base: a commit, or the index (no `sha`)."""

    label: str
    sha: str | None


class LoaderChange(NamedTuple):
    """What a loader changed: its violations, and the lines it inserted.

    `inserted` is `None` when the content was not compared.
    """

    violations: list[str]
    inserted: list[str] | None


def run_git(*args: str) -> str:
    """Run a read-only git command in the repository and return its stdout."""
    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
    try:
        result = subprocess.run(  # noqa: S603 - fixed git argv.
            ['git', '-C', str(REPO_ROOT), *args],  # noqa: S607
            capture_output=True,
            env=env,
            check=False,
        )
    except OSError as exc:
        raise GitError(f'git is not available ({type(exc).__name__})') from exc

    if result.returncode != 0:
        raise GitError(f'git {args[0]} exited with {result.returncode}')

    return _decode(result.stdout)


def check_core(base: str) -> list[str]:
    """Return violations of the Core/build boundary against `base`.

    Judge the index and every commit above `base`, never the working tree.
    """
    try:
        base_sha = _resolve_base(base)
        states, _ = _states(base_sha)
        return [
            violation
            for state in states
            for violation in _core_violations(base_sha, state)
        ]
    except GitError as exc:
        return [str(exc)]


def check_catalogues(base: str) -> list[str]:
    """Return violations of the `.po`-only catalogue rule against `base`."""
    try:
        base_sha = _resolve_base(base)
        states, tips = _states(base_sha)
        return (
            _catalogue_history_violations(base_sha)
            + [
                violation
                for state in tips
                for violation in _catalogue_state_violations(base_sha, state)
            ]
            + [
                violation
                for state in states
                for violation in _compile_guidance_violations(base_sha, state)
            ]
        )
    except GitError as exc:
        return [str(exc)]


def main(argv: list[str] | None = None) -> int:
    """Run the selected checks, print one verdict line each, and exit code."""
    parser = argparse.ArgumentParser(
        description='Verify the chair module leaves Core and catalogues alone.'
    )
    parser.add_argument('--base', default='main')
    parser.add_argument(
        '--check', choices=['core', 'catalogues', 'all'], default='all'
    )
    args = parser.parse_args(argv)

    checks = {'core': check_core, 'catalogues': check_catalogues}
    names = list(checks) if args.check == 'all' else [args.check]

    failed = False
    with _private_index():
        for name in names:
            violations = checks[name](args.base)
            print(f'{"FAIL" if violations else "PASS"}: {name}')
            for violation in violations:
                print(f'  {violation}')
            failed = failed or bool(violations)

    return 1 if failed else 0


@contextmanager
def _private_index() -> Iterator[None]:
    """Point git at a copy of the index, so no call can write the real one."""
    try:
        index = REPO_ROOT / run_git('rev-parse', '--git-path', 'index').strip()
    except GitError:
        index = None

    if index is None or not index.is_file():
        yield
        return

    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / 'index'
        shutil.copyfile(index, copy)

        previous = os.environ.get('GIT_INDEX_FILE')
        os.environ['GIT_INDEX_FILE'] = str(copy)
        try:
            yield
        finally:
            if previous is None:
                del os.environ['GIT_INDEX_FILE']
            else:
                os.environ['GIT_INDEX_FILE'] = previous


def _decode(data: bytes) -> str:
    return data.decode('utf-8', errors='surrogateescape')


def _resolve_base(base: str) -> str:
    if base.startswith('-'):
        raise GitError(f'cannot resolve base {base!r}')

    try:
        return run_git('rev-parse', '--verify', f'{base}^{{commit}}').strip()
    except GitError as exc:
        raise GitError(f'cannot resolve base {base!r}') from exc


def _paths(output: str) -> set[str]:
    return {path for path in output.split('\0') if path}


def _is_metadata_file(path: str) -> bool:
    return path.rsplit('/', 1)[-1] in METADATA_FILES


def _is_core(path: str) -> bool:
    if _is_metadata_file(path):
        return True

    return path not in EXEMPT_PATHS and not path.startswith(EXEMPT_PREFIXES)


def _describe_core_path(path: str) -> str:
    if path.startswith(STATIC_PREFIX):
        return f'Core static asset changed: {path} (must match base)'
    if path.startswith(MORE_PREFIX):
        return f'More service changed: {path} (must match base)'
    if path == TICKET_TEMPLATE:
        return f'Core ticket template changed: {path} (must match base)'
    if path in BUILD_FILES:
        return f'build file changed: {path} (must match base)'
    return f'unapproved Core path changed: {path}'


def _states(base_sha: str) -> tuple[list[State], list[State]]:
    """Return the states to judge, and the tips (index and HEAD) among them.

    The index comes first, then every commit in `base..HEAD`. An index equal
    to HEAD is reported once, as `index/HEAD`.
    """
    head = run_git('rev-parse', '--verify', 'HEAD^{commit}').strip()
    commits = run_git('rev-list', f'{base_sha}..HEAD').split()
    others = [State(commit[:9], commit) for commit in commits if commit != head]

    if not _index_differs_from(head):
        tip = State('index/HEAD', head)
        return [tip, *others], [tip]

    index = State('index', None)
    tip = State(head[:9], head)
    in_range = [tip] if head in commits else []
    return [index, *in_range, *others], [index, tip]


def _index_differs_from(commit: str) -> bool:
    changed = run_git(
        'diff',
        '--cached',
        '--name-only',
        *PATH_DIFF_FLAGS,
        '-z',
        commit,
        '--',
    )
    return bool(_paths(changed))


def _prefixed(state: State, violations: list[str]) -> list[str]:
    return [f'{state.label}: {violation}' for violation in violations]


def _revision(state: State, path: str) -> str:
    """Return the object name of `path` in `state`, as `cat-file` takes it."""
    return f':{path}' if state.sha is None else f'{state.sha}:{path}'


def _state_diff(
    base_sha: str, state: State, *options: str, pathspecs: tuple[str, ...] = ()
) -> str:
    """Run `git diff` of `state` against base, reading no working tree file.

    The index is read with `--cached`; a commit is named, so its tree is read.
    """
    if state.sha is None:
        argv = ['diff', '--cached', *options, base_sha, '--']
    else:
        argv = ['diff', *options, base_sha, state.sha, '--']
    return run_git(*argv, *pathspecs)


def _entries(state: State, path: str) -> list[tuple[str, str]]:
    """Return the `(mode, stage)` of each entry git lists for `path`."""
    if state.sha is None:
        command, output = 'ls-files', run_git('ls-files', '-s', '--', path)
    else:
        command, output = 'ls-tree', run_git('ls-tree', state.sha, '--', path)

    entries = []
    for line in output.split('\n'):
        if not line:
            continue

        meta, _, name = line.partition('\t')
        fields = meta.split()
        if len(fields) != 3:
            raise GitError(f'unexpected output from git {command}')

        if name == path:
            entries.append((fields[0], fields[2] if state.sha is None else '0'))

    return entries


def _is_unmerged(entries: list[tuple[str, str]]) -> bool:
    return any(stage != '0' for _, stage in entries)


def _core_violations(base_sha: str, state: State) -> list[str]:
    changed = _paths(
        _state_diff(base_sha, state, '--name-only', *PATH_DIFF_FLAGS, '-z')
    )
    paths = sorted(p for p in changed if _is_core(p))

    loaders = {
        path: _loader_change(base_sha, state, path)
        for path in paths
        if path in LOADER_LINES
    }
    inserted = {
        path: change.inserted
        for path, change in loaders.items()
        if change.inserted is not None
    }
    allowed = _choose_loader_lines(inserted)

    violations: list[str] = []
    for path in paths:
        if path in loaders:
            violations.extend(loaders[path].violations)
            if path in inserted:
                violations.extend(
                    _line_violations(path, inserted[path], allowed[path])
                )
        else:
            violations.append(_describe_core_path(path))
    violations.extend(_mixed_set_violations(inserted))

    return _prefixed(state, violations)


def _loader_change(base_sha: str, state: State, path: str) -> LoaderChange:
    """Compare the loader in `state` with base, raw blob against raw blob.

    No attribute, filter or comment can hide a change. The mode must be the
    base mode: a symlink or a gitlink has no content to review, and an exec
    bit is a change. Inserted lines are judged by the caller, against one set.
    """
    entries = _entries(state, path)
    if not entries:
        return LoaderChange([f'{path}: is deleted'], None)
    if _is_unmerged(entries):
        return LoaderChange([f'{path}: has unmerged index entries'], None)

    base_entries = _entries(State('base', base_sha), path)
    if not base_entries:
        raise GitError(f'{path} is missing at base')

    mode, base_mode = entries[0][0], base_entries[0][0]
    violations = []
    if mode != base_mode:
        violations.append(
            f'{path}: file mode changed from {base_mode} to {mode}'
        )
    if mode not in REGULAR_MODES:
        return LoaderChange(violations, None)

    base = _lines(run_git('cat-file', 'blob', f'{base_sha}:{path}'))
    new = _lines(run_git('cat-file', 'blob', _revision(state, path)))

    inserted: list[str] = []
    matcher = difflib.SequenceMatcher(None, base, new, autojunk=False)
    for tag, base_from, base_to, new_from, new_to in matcher.get_opcodes():
        if tag != 'equal':
            violations.extend(
                f'{path}: removes a line: {line!r}'
                for line in base[base_from:base_to]
            )
            inserted.extend(new[new_from:new_to])

    return LoaderChange(violations, inserted)


def _line_violations(
    path: str, inserted: list[str], allowed: tuple[str, ...]
) -> list[str]:
    """Require `inserted` to be exactly the lines of one loader set."""
    violations = []
    missing = Counter(allowed)
    for line in inserted:
        if missing[line] > 0:
            missing[line] -= 1
        elif line in allowed:
            violations.append(f'{path}: adds a repeated loader line: {line!r}')
        else:
            violations.append(f'{path}: adds a non-chair line: {line!r}')

    violations.extend(
        f'{path}: lacks the chair loader line: {line!r}'
        for line in missing.elements()
    )
    return violations


def _choose_loader_lines(
    inserted: dict[str, list[str]],
) -> dict[str, tuple[str, ...]]:
    """Return the one loader line set the changed loaders come closest to."""

    def count_violations(line_set: dict[str, tuple[str, ...]]) -> int:
        return sum(
            len(_line_violations(path, lines, line_set[path]))
            for path, lines in inserted.items()
        )

    return min(LOADER_LINE_SETS.values(), key=count_violations)


def _mixed_set_violations(inserted: dict[str, list[str]]) -> list[str]:
    """Report loaders that each fit a loader set, but not the same one."""
    fitting = {
        path: name
        for path, lines in inserted.items()
        for name, line_set in LOADER_LINE_SETS.items()
        if not _line_violations(path, lines, line_set[path])
    }
    if len(set(fitting.values())) < 2:
        return []

    return [
        'loader lines mix the legacy and the current chair sets: '
        + ', '.join(
            f'{path} uses the {name} set'
            for path, name in sorted(fitting.items())
        )
    ]


def _lines(text: str) -> list[str]:
    return text.split('\n')


def _diff_against(base_sha: str, state: State, *paths: str) -> str:
    return _state_diff(
        base_sha,
        state,
        '-U0',
        '--no-color',
        '--no-ext-diff',
        '--text',
        '--no-textconv',
        '--ignore-submodules=none',
        pathspecs=paths,
    )


def _diff_lines(diff: str) -> tuple[list[str], list[str]]:
    """Split a unified diff into removed and added lines, skipping headers.

    Split on `\\n` only, as git does: `splitlines` would cut an added line at
    a lone `\\r` and skip the tail, which has no `+` prefix.
    """
    removed: list[str] = []
    added: list[str] = []
    in_hunk = False
    for line in diff.split('\n'):
        if line.startswith('diff --git'):
            in_hunk = False
        elif line.startswith('@@'):
            in_hunk = True
        elif in_hunk and line.startswith('-'):
            removed.append(line[1:])
        elif in_hunk and line.startswith('+'):
            added.append(line[1:])
    return removed, added


def _catalogue_history_violations(base_sha: str) -> list[str]:
    violations: list[str] = []
    for commit in run_git('rev-list', f'{base_sha}..HEAD').split():
        touched = _paths(
            run_git(
                'diff-tree',
                '-r',
                '-m',
                '--root',
                *PATH_DIFF_FLAGS,
                '--no-commit-id',
                '--name-only',
                '-z',
                commit,
                '--',
                TRANSLATIONS_PREFIX,
            )
        )
        violations.extend(
            f'{commit[:9]}: touched generated catalogue {path}'
            for path in sorted(touched)
            if not path.endswith('.po')
        )
    return violations


def _catalogue_state_violations(base_sha: str, state: State) -> list[str]:
    changed = _paths(
        _state_diff(
            base_sha,
            state,
            '--name-only',
            *PATH_DIFF_FLAGS,
            '-z',
            pathspecs=(TRANSLATIONS_PREFIX,),
        )
    )

    return _prefixed(
        state,
        [
            f'stale generated catalogue: {path} differs from base'
            for path in sorted(changed)
            if not path.endswith('.po')
        ],
    )


def _compile_guidance_violations(base_sha: str, state: State) -> list[str]:
    violations = _readme_violations(state)

    _, added = _diff_lines(_diff_against(base_sha, state, *BUILD_COMPILE_PATHS))
    violations.extend(
        f'build file adds catalogue compile step: {line.strip()!r}'
        for line in added
        if BUILD_COMPILE_PATTERN.search(line)
    )

    return _prefixed(state, violations)


def _readme_violations(state: State) -> list[str]:
    """Scan the README blob of `state`; a link or gitlink has no text to scan.

    The README is at the legacy or the current path, or at both.
    """
    violations: list[str] = []
    for path in README_PATHS:
        violations.extend(_readme_path_violations(state, path))
    return violations


def _readme_path_violations(state: State, path: str) -> list[str]:
    entries = _entries(state, path)
    if not entries:
        return []
    if _is_unmerged(entries):
        return [f'{path}: has unmerged index entries']
    if entries[0][0] not in REGULAR_MODES:
        return [f'{path}: is not a regular file']

    readme = run_git('cat-file', 'blob', _revision(state, path))
    return [
        f'{path}:{number}: local catalogue compile guidance'
        for number, line in enumerate(_lines(readme), start=1)
        if README_COMPILE_PATTERN.search(line)
    ]


if __name__ == '__main__':
    sys.exit(main())
