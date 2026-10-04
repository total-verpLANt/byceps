from collections import Counter
import difflib
import os
from pathlib import Path
import shutil
import subprocess
from typing import NamedTuple

import pytest

from byceps.services.chair_planning.scripts import (
    check_core_boundary as boundary,
)


BASE = 'main'
BASE_SHA = 'b' * 40
FIRST_COMMIT = '1' * 40
SECOND_COMMIT = '2' * 40
INDEX = 'index'

PO = 'byceps/translations/de/LC_MESSAGES/messages.po'
MO = 'byceps/translations/de/LC_MESSAGES/messages.mo'

READ_ONLY_COMMANDS = frozenset(
    {
        'rev-parse',
        'rev-list',
        'diff',
        'diff-tree',
        'ls-files',
        'ls-tree',
        'cat-file',
    }
)
CONTENT_DIFF_FLAGS = frozenset({'--no-ext-diff', '--text', '--no-textconv'})

# Where a change lives: the index only, the first (non-tip) commit only, or
# HEAD, which the index equals.
WHERE_TREES = {
    'index': [INDEX],
    'commit': [FIRST_COMMIT],
    'head': [SECOND_COMMIT, INDEX],
}
LABELS = {
    'index': 'index',
    'commit': FIRST_COMMIT[:9],
    'head': 'index/HEAD',
}

LOADER_BASE = {
    'byceps/application.py': [
        'from byceps.database import db',
        '',
        '    db.init_app(app)',
        '    configure_app(app)',
        '-- legacy marker',
    ],
    'byceps/blueprints/admin.py': [
        "        ('services.page.blueprints.admin', '/pages'),",
        "        ('services.seating.blueprints.admin', '/seating'),",
    ],
    'byceps/blueprints/site.py': [
        "        ('services.news.blueprints.site', '/news'),",
        "        ('services.seating.blueprints.site', '/seating'),",
    ],
}

LOADER_ADDITIONS = {
    'byceps/application.py': [
        'from byceps.services.chair_planning.lifecycle import enable_chair_lifecycle',
        '',
        '    enable_chair_lifecycle()',
    ],
    'byceps/blueprints/admin.py': [
        "        ('services.chair_planning.blueprints.admin', '/chair_planning'),",
    ],
    'byceps/blueprints/site.py': [
        "        ('services.chair_planning.blueprints.site', '/chair_planning'),",
    ],
}
# What the history before the rename carries.
LEGACY_LOADER_ADDITIONS = {
    'byceps/application.py': [
        'from byceps.services.chair_optout.lifecycle import enable_chair_lifecycle',
        '',
        '    enable_chair_lifecycle()',
    ],
    'byceps/blueprints/admin.py': [
        "        ('services.chair_optout.blueprints.admin', '/chair_optout'),",
    ],
    'byceps/blueprints/site.py': [
        "        ('services.chair_optout.blueprints.site', '/chair_optout'),",
    ],
}
LEGACY_PREFIX = 'byceps/services/chair_optout/'
PREFIX = 'byceps/services/chair_planning/'

CLEAN_README = 'Catalogues are compiled on the server.\n'


class Entry(NamedTuple):
    mode: str
    content: str


def regular(content):
    return Entry('100644', content)


def split_lines(content):
    lines = content.split('\n')
    if lines[-1] == '':
        lines.pop()
    return lines


def make_diff(path, old, new):
    """Render a `-U0` diff of two entries, splitting on `\\n` only."""
    old_lines = split_lines(old.content) if old else []
    new_lines = split_lines(new.content) if new else []
    lines = [
        f'diff --git a/{path} b/{path}',
        'index 1111111..2222222 100644',
        f'--- a/{path}',
        f'+++ b/{path}',
    ]
    matcher = difflib.SequenceMatcher(
        None, old_lines, new_lines, autojunk=False
    )
    for tag, old_from, old_to, new_from, new_to in matcher.get_opcodes():
        if tag == 'equal':
            continue
        lines.append(
            f'@@ -{old_from + 1},{old_to - old_from} '
            f'+{new_from + 1},{new_to - new_from} @@'
        )
        lines.extend(f'-{line}' for line in old_lines[old_from:old_to])
        lines.extend(f'+{line}' for line in new_lines[new_from:new_to])
    return '\n'.join(lines) + '\n'


def select(paths, pathspecs):
    return sorted(
        path
        for path in paths
        if not pathspecs
        or any(
            path == spec or path.startswith(spec.rstrip('/') + '/')
            for spec in pathspecs
        )
    )


def join_lines(lines):
    return '\n'.join(lines) + '\n'


def nul_separated(paths):
    return ''.join(f'{path}\0' for path in paths)


def differing(old, new):
    return {
        path
        for path in old.keys() | new.keys()
        if old.get(path) != new.get(path)
    }


class FakeGit:
    """Stand in for `run_git`: answers read-only git calls from tree fixtures.

    A tree maps a path to an `Entry`. The trees are base, two commits and the
    index. By default the branch is a clean feature: the first commit adds the
    exact chair loaders, a `.po` change and the README, the second changes
    nothing more, and the index equals HEAD (the second commit).
    """

    def __init__(self):
        self.base = BASE
        self.head = SECOND_COMMIT
        self.log = [SECOND_COMMIT, FIRST_COMMIT]
        self.parents = {FIRST_COMMIT: [BASE_SHA], SECOND_COMMIT: [FIRST_COMMIT]}
        self.unmerged = set()
        self.entry_output = {}
        self.strip_final_newline = False
        self.calls = []

        base = {
            path: regular(join_lines(lines))
            for path, lines in LOADER_BASE.items()
        }
        base['Dockerfile'] = regular('FROM python:3.13\n')
        base[PO] = regular('msgid "a"\n')
        base[MO] = regular('compiled\n')

        feature = dict(base)
        for path, added in LOADER_ADDITIONS.items():
            feature[path] = regular(join_lines([*LOADER_BASE[path], *added]))
        feature[PO] = regular('msgid "a"\nmsgid "chair"\n')
        feature[boundary.README_PATH] = regular(CLEAN_README)

        self.trees = {
            BASE_SHA: base,
            FIRST_COMMIT: feature,
            SECOND_COMMIT: dict(feature),
            INDEX: dict(feature),
        }

    def change(self, where, path, content, *, mode='100644'):
        """Set `path` in the trees `where` names; `None` deletes it."""
        for key in WHERE_TREES[where]:
            if content is None:
                del self.trees[key][path]
            else:
                self.trees[key][path] = Entry(mode, content)

    def set_loader(self, where, path, removed=(), added=()):
        lines = list(LOADER_BASE[path])
        for line in removed:
            lines.remove(line)
        self.change(where, path, join_lines([*lines, *added]))

    def add_lines(self, where, path, lines):
        old = self.trees[BASE_SHA].get(path)
        content = (old.content if old else '') + join_lines(lines)
        self.change(where, path, content)

    def __call__(self, *args):
        self.calls.append(args)
        command, *rest = args
        assert command in READ_ONLY_COMMANDS, args
        assert not any('...' in arg for arg in args), args
        handler = getattr(self, 'git_' + command.replace('-', '_'))
        return handler(*rest)

    def git_rev_parse(self, *args):
        if args == ('--git-path', 'index'):
            return '.git/index\n'
        assert args[0] == '--verify', args
        if args[1] == 'HEAD^{commit}':
            return self.head + '\n'
        if args[1] != f'{self.base}^{{commit}}':
            raise boundary.GitError('unknown revision')
        return BASE_SHA + '\n'

    def git_rev_list(self, *args):
        assert args == (f'{BASE_SHA}..HEAD',), args
        return ''.join(f'{commit}\n' for commit in self.log)

    def git_diff(self, *args):
        separator = args.index('--')
        options, pathspecs = args[:separator], args[separator + 1 :]
        assert '--ignore-submodules=none' in options, args
        revisions = [arg for arg in options if not arg.startswith('-')]
        if '--cached' in options:
            assert len(revisions) == 1, args
            left, right = revisions[0], INDEX
        else:
            assert len(revisions) == 2, args
            left, right = revisions
        old, new = self.trees[left], self.trees[right]
        changed = select(differing(old, new), pathspecs)

        if '--name-only' in options:
            assert {'--no-renames', '-z'} <= set(options), args
            return nul_separated(changed)

        assert '-U0' in options, args
        assert CONTENT_DIFF_FLAGS <= set(options), args
        text = ''.join(
            make_diff(path, old.get(path), new.get(path)) for path in changed
        )
        return text.rstrip('\n') if self.strip_final_newline else text

    def git_diff_tree(self, *args):
        separator = args.index('--')
        options, pathspecs = args[:separator], args[separator + 1 :]
        assert {
            '-r',
            '-m',
            '--root',
            '--no-renames',
            '--ignore-submodules=none',
            '--no-commit-id',
            '--name-only',
            '-z',
        } <= set(options), args
        commit = options[-1]
        touched = set()
        for parent in self.parents[commit]:
            touched |= differing(self.trees[parent], self.trees[commit])
        return nul_separated(select(touched, pathspecs))

    def git_ls_files(self, *args):
        assert args[:2] == ('-s', '--'), args
        (path,) = args[2:]
        if (INDEX, path) in self.entry_output:
            return self.entry_output[INDEX, path]
        if path in self.unmerged:
            return ''.join(f'100644 {"a" * 40} {s}\t{path}\n' for s in '123')
        # Like git, list everything below a directory pathspec.
        return ''.join(
            f'{entry.mode} {"a" * 40} 0\t{name}\n'
            for name, entry in sorted(self.trees[INDEX].items())
            if name == path or name.startswith(path + '/')
        )

    def git_ls_tree(self, *args):
        sha, separator, path = args
        assert separator == '--', args
        if (sha, path) in self.entry_output:
            return self.entry_output[sha, path]
        tree = self.trees[sha]
        if path in tree:
            kind = 'commit' if tree[path].mode == '160000' else 'blob'
            return f'{tree[path].mode} {kind} {"a" * 40}\t{path}\n'
        if any(name.startswith(path + '/') for name in tree):
            return f'040000 tree {"a" * 40}\t{path}\n'
        return ''

    def git_cat_file(self, *args):
        assert args[0] == 'blob', args
        sha, _, path = args[1].partition(':')
        entry = self.trees[sha or INDEX].get(path)
        if entry is None:
            raise boundary.GitError('git cat-file exited with 128')
        assert entry.mode in {'100644', '100755', '120000'}, args
        return entry.content


@pytest.fixture
def fake_git(monkeypatch, tmp_path):
    fake = FakeGit()
    monkeypatch.setattr(boundary, 'run_git', fake)
    monkeypatch.setattr(boundary, 'REPO_ROOT', tmp_path)
    return fake


@pytest.fixture(params=['index', 'commit'])
def where(request):
    return request.param


def test_boundary_accepts_loading_and_po_only(fake_git):
    for place in ('commit', 'head'):
        for path in (
            'byceps/services/chair_planning/lifecycle.py',
            'byceps/services/chair_planning/blueprints/admin/navigation.py',
            'byceps/services/chair_optout/lifecycle.py',
            'byceps/services/chair_optout/blueprints/admin/navigation.py',
            'uv.lock',
            'sites/totalverplant/static/logo.svg',
            'tests/unit/services/chair_planning/test_service.py',
            'tests/unit/services/chair_optout/test_service.py',
        ):
            fake_git.change(place, path, 'x\n')

    assert boundary.check_core(BASE) == []
    assert boundary.check_catalogues(BASE) == []


# fmt: off
@pytest.mark.parametrize(
    'path',
    [
        'byceps/services/ticketing/ticket_service.py',
        'byceps/services/ticketing/new_helper.py',
        'byceps/blueprints/common.py',
        'byceps/blueprints/admin_extras.py',
    ],
    ids=['edited', 'new-file', 'sibling-blueprint', 'new-sibling'],
)
# fmt: on
def test_boundary_rejects_unapproved_core_path(fake_git, where, path):
    fake_git.change(where, path, 'x = 1\n')

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: unapproved Core path changed: {path}'
    ]


def test_boundary_rejects_intermediate_mo_change_even_if_head_is_clean(
    fake_git,
):
    fake_git.change('commit', MO, 'compiled-local\n')

    assert fake_git.trees[INDEX][MO] == fake_git.trees[BASE_SHA][MO]
    assert boundary.check_core(BASE) == []
    assert boundary.check_catalogues(BASE) == [
        f'{SECOND_COMMIT[:9]}: touched generated catalogue {MO}',
        f'{FIRST_COMMIT[:9]}: touched generated catalogue {MO}',
    ]


def test_boundary_history_diffs_cover_merge_commits(fake_git):
    boundary.check_catalogues(BASE)

    history_calls = [c for c in fake_git.calls if c[0] == 'diff-tree']
    assert len(history_calls) == len(fake_git.log)
    assert all('-m' in call and '--no-renames' in call for call in history_calls)


def test_boundary_history_covers_every_parent_of_a_merge(fake_git):
    # The merge equals its first parent, and differs from its second parent.
    fake_git.parents[SECOND_COMMIT] = [FIRST_COMMIT, BASE_SHA]
    for key in (FIRST_COMMIT, SECOND_COMMIT, INDEX):
        fake_git.trees[key][MO] = regular('compiled-local\n')

    assert boundary.check_catalogues(BASE) == [
        f'{SECOND_COMMIT[:9]}: touched generated catalogue {MO}',
        f'{FIRST_COMMIT[:9]}: touched generated catalogue {MO}',
        f'index/HEAD: stale generated catalogue: {MO} differs from base',
    ]


# fmt: off
@pytest.mark.parametrize(
    'location',
    ['index', 'head'],
)
# fmt: on
def test_boundary_rejects_stale_generated_catalogue(fake_git, location):
    fake_git.change(location, MO, 'compiled-local\n')

    stale = (
        f'{LABELS[location]}: stale generated catalogue: {MO} differs from base'
    )
    history = [f'{SECOND_COMMIT[:9]}: touched generated catalogue {MO}']
    assert boundary.check_catalogues(BASE) == (
        [stale] if location == 'index' else [*history, stale]
    )


def test_boundary_judges_head_when_the_index_hides_its_stale_catalogue(
    fake_git,
):
    fake_git.change('head', MO, 'compiled-local\n')
    fake_git.trees[INDEX][MO] = fake_git.trees[BASE_SHA][MO]

    assert boundary.check_catalogues(BASE) == [
        f'{SECOND_COMMIT[:9]}: touched generated catalogue {MO}',
        (
            f'{SECOND_COMMIT[:9]}: stale generated catalogue: {MO} '
            'differs from base'
        ),
    ]


# fmt: off
@pytest.mark.parametrize('location', ['index', 'head'])
# fmt: on
def test_boundary_rejects_a_deleted_generated_catalogue(fake_git, location):
    fake_git.change(location, MO, None)

    assert (
        f'{LABELS[location]}: stale generated catalogue: {MO} differs from base'
    ) in boundary.check_catalogues(BASE)


def test_boundary_rejects_core_asset_change(fake_git, where):
    path = 'byceps/static/style/default/admin.css'
    fake_git.change(where, path, 'a { color: red; }\n')

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: Core static asset changed: {path} (must match base)'
    ]


# fmt: off
@pytest.mark.parametrize(
    ('path', 'message'),
    [
        (
            'byceps/services/more/blueprints/admin/item_service.py',
            'More service changed',
        ),
        (boundary.TICKET_TEMPLATE, 'Core ticket template changed'),
        ('Dockerfile', 'build file changed'),
        ('justfile', 'build file changed'),
        ('.dockerignore', 'build file changed'),
        ('pyproject.toml', 'build file changed'),
    ],
)
# fmt: on
def test_boundary_rejects_protected_core_paths(fake_git, where, path, message):
    fake_git.change(where, path, 'x\n')

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: {message}: {path} (must match base)'
    ]


# fmt: off
@pytest.mark.parametrize(
    ('path', 'removed', 'added', 'expected'),
    [
        (
            'byceps/application.py',
            ['    configure_app(app)'],
            [],
            "byceps/application.py: removes a line: '    configure_app(app)'",
        ),
        (
            'byceps/application.py',
            ['-- legacy marker'],
            [],
            "byceps/application.py: removes a line: '-- legacy marker'",
        ),
        (
            'byceps/blueprints/admin.py',
            [],
            ["        ('services.other', '/other'),"],
            (
                "byceps/blueprints/admin.py: adds a non-chair line: "
                "\"        ('services.other', '/other'),\""
            ),
        ),
        (
            'byceps/blueprints/site.py',
            ["        ('services.news.blueprints.site', '/news'),"],
            ["        ('services.chair_planning.blueprints.site', '/chair'),"],
            (
                "byceps/blueprints/site.py: removes a line: "
                "\"        ('services.news.blueprints.site', '/news'),\""
            ),
        ),
    ],
    ids=['removed', 'removed-dashes', 'added-non-chair', 'swapped-guard'],
)
# fmt: on
def test_boundary_rejects_loader_guard_churn(
    fake_git, where, path, removed, added, expected
):
    fake_git.set_loader(where, path, removed=removed, added=added)

    assert f'{LABELS[where]}: {expected}' in boundary.check_core(BASE)


# fmt: off
@pytest.mark.parametrize(
    'readme_line',
    [
        'Run `just babel-compile` after pulling.',
        '    pybabel compile -d byceps/translations',
    ],
    ids=['just', 'pybabel'],
)
# fmt: on
def test_boundary_rejects_local_compile_guidance_in_readme(
    fake_git, where, readme_line
):
    fake_git.change(where, boundary.README_PATH, f'Setup\n{readme_line}\n')

    assert boundary.check_catalogues(BASE) == [
        (
            f'{LABELS[where]}: {boundary.README_PATH}:2: '
            'local catalogue compile guidance'
        )
    ]


def test_boundary_numbers_readme_lines_as_git_does(fake_git, where):
    fake_git.change(
        where, boundary.README_PATH, 'a\rb\nRun `just babel-compile`.\n'
    )

    assert boundary.check_catalogues(BASE) == [
        (
            f'{LABELS[where]}: {boundary.README_PATH}:2: '
            'local catalogue compile guidance'
        )
    ]


def test_boundary_scans_the_readme_blob_not_the_working_file(
    fake_git, tmp_path
):
    readme = tmp_path / boundary.README_PATH
    readme.parent.mkdir(parents=True)
    readme.write_text('Run `just babel-compile`.\n')

    assert boundary.check_catalogues(BASE) == []


def test_boundary_accepts_a_state_without_a_readme(fake_git):
    fake_git.change('commit', boundary.README_PATH, None)

    assert boundary.check_catalogues(BASE) == []


# fmt: off
@pytest.mark.parametrize('mode', ['120000', '160000'], ids=['link', 'gitlink'])
# fmt: on
def test_boundary_rejects_a_readme_that_is_not_a_regular_file(
    fake_git, where, mode
):
    fake_git.change(where, boundary.README_PATH, 'x', mode=mode)

    assert boundary.check_catalogues(BASE) == [
        f'{LABELS[where]}: {boundary.README_PATH}: is not a regular file'
    ]


def test_boundary_scans_the_legacy_readme_path(fake_git, where):
    fake_git.change(
        where, boundary.LEGACY_README_PATH, 'Run `just babel-compile`.\n'
    )

    assert boundary.check_catalogues(BASE) == [
        (
            f'{LABELS[where]}: {boundary.LEGACY_README_PATH}:1: '
            'local catalogue compile guidance'
        )
    ]


def test_boundary_accepts_a_state_with_only_the_legacy_readme(fake_git, where):
    fake_git.change(where, boundary.README_PATH, None)
    fake_git.change(where, boundary.LEGACY_README_PATH, CLEAN_README)

    assert boundary.check_catalogues(BASE) == []


def test_boundary_scans_both_readme_paths_when_both_exist(fake_git, where):
    advice = 'Setup\nRun `just babel-compile`.\n'
    fake_git.change(where, boundary.LEGACY_README_PATH, advice)
    fake_git.change(where, boundary.README_PATH, advice)

    assert boundary.check_catalogues(BASE) == [
        (
            f'{LABELS[where]}: {boundary.LEGACY_README_PATH}:2: '
            'local catalogue compile guidance'
        ),
        (
            f'{LABELS[where]}: {boundary.README_PATH}:2: '
            'local catalogue compile guidance'
        ),
    ]


def test_boundary_scans_the_current_readme_beside_a_clean_legacy_one(
    fake_git, where
):
    fake_git.change(where, boundary.LEGACY_README_PATH, CLEAN_README)
    fake_git.change(
        where, boundary.README_PATH, 'Run `just babel-compile`.\n'
    )

    assert boundary.check_catalogues(BASE) == [
        (
            f'{LABELS[where]}: {boundary.README_PATH}:1: '
            'local catalogue compile guidance'
        )
    ]


# fmt: off
@pytest.mark.parametrize('mode', ['120000', '160000'], ids=['link', 'gitlink'])
# fmt: on
def test_boundary_rejects_a_legacy_readme_that_is_not_a_regular_file(
    fake_git, where, mode
):
    fake_git.change(where, boundary.LEGACY_README_PATH, 'x', mode=mode)

    assert boundary.check_catalogues(BASE) == [
        f'{LABELS[where]}: {boundary.LEGACY_README_PATH}: is not a regular file'
    ]


# fmt: off
@pytest.mark.parametrize(
    ('path', 'added'),
    [
        ('Dockerfile', 'RUN pybabel compile -d byceps/translations'),
        ('Dockerfile', 'RUN msgfmt de.po -o de.mo'),
        ('justfile', 'babel-compile:'),
        ('justfile', '    python setup.py compile_catalog'),
        ('compose.yaml', '    command: pybabel compile -d byceps/translations'),
        ('docker/byceps/entrypoint.sh', 'pybabel compile -d byceps/translations'),
        ('docker/byceps/Dockerfile.dev', 'RUN msgfmt de.po -o de.mo'),
        ('babel.cfg', '# pybabel compile -d byceps/translations'),
    ],
)
# fmt: on
def test_boundary_rejects_local_compile_guidance_in_build_files(
    fake_git, where, path, added
):
    fake_git.add_lines(where, path, [added])

    assert boundary.check_catalogues(BASE) == [
        (
            f'{LABELS[where]}: build file adds catalogue compile step: '
            f'{added.strip()!r}'
        )
    ]


def test_boundary_rejects_local_compile_guidance(fake_git, where):
    fake_git.change(where, boundary.README_PATH, 'Run `just babel-compile`.\n')
    fake_git.add_lines(
        where, 'Dockerfile', ['RUN pybabel compile -d byceps/translations']
    )

    violations = boundary.check_catalogues(BASE)

    assert len(violations) == 2
    assert 'local catalogue compile guidance' in violations[0]
    assert 'build file adds catalogue compile step' in violations[1]
    assert all(v.startswith(f'{LABELS[where]}: ') for v in violations)


@pytest.mark.parametrize('base', ['does-not-exist', '--output=leak'])
def test_boundary_rejects_unresolvable_base(fake_git, base):
    expected = [f'cannot resolve base {base!r}']

    assert boundary.check_core(base) == expected
    assert boundary.check_catalogues(base) == expected
    assert all(call[0] == 'rev-parse' for call in fake_git.calls)
    if base.startswith('-'):
        assert fake_git.calls == []


def test_run_git_is_read_only_and_never_leaks_stderr(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen['argv'] = argv
        seen['env'] = kwargs['env']
        return subprocess.CompletedProcess(argv, 0, stdout=b'out\n', stderr=b'')

    monkeypatch.setattr(boundary.subprocess, 'run', fake_run)

    assert boundary.run_git('rev-parse', 'HEAD') == 'out\n'
    assert seen['argv'] == [
        'git',
        '-C',
        str(boundary.REPO_ROOT),
        'rev-parse',
        'HEAD',
    ]
    assert seen['env']['GIT_OPTIONAL_LOCKS'] == '0'

    def failing_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 128, stdout=b'', stderr=b'fatal: https://user:s3cret@host/x'
        )

    monkeypatch.setattr(boundary.subprocess, 'run', failing_run)

    with pytest.raises(boundary.GitError) as excinfo:
        boundary.run_git('diff')

    assert 's3cret' not in str(excinfo.value)


def test_cli_returns_nonzero_for_violation(fake_git, capsys):
    assert boundary.main(['--base', BASE]) == 0
    output = capsys.readouterr().out
    assert 'PASS: core' in output
    assert 'PASS: catalogues' in output
    assert 'FAIL' not in output

    path = 'byceps/services/ticketing/ticket_service.py'
    fake_git.change('index', path, 'x = 1\n')

    assert boundary.main(['--base', BASE]) == 1
    output = capsys.readouterr().out
    assert 'FAIL: core' in output
    assert f'  index: unapproved Core path changed: {path}' in output
    assert 'PASS: catalogues' in output


def test_cli_runs_only_the_selected_check(fake_git, capsys):
    fake_git.change('index', MO, 'compiled-local\n')

    assert boundary.main(['--check', 'core']) == 0
    assert capsys.readouterr().out == 'PASS: core\n'

    assert boundary.main(['--check', 'catalogues']) == 1
    output = capsys.readouterr().out
    assert output.splitlines()[0] == 'FAIL: catalogues'
    assert 'PASS' not in output


def test_cli_fails_for_unresolvable_base(fake_git, capsys):
    assert boundary.main(['--base', 'does-not-exist']) == 1
    output = capsys.readouterr().out
    assert 'FAIL: core' in output
    assert 'FAIL: catalogues' in output


def test_cli_rejects_an_unknown_check_with_the_usage_exit_code(fake_git):
    with pytest.raises(SystemExit) as excinfo:
        boundary.main(['--check', 'everything'])

    assert excinfo.value.code == 2
    assert fake_git.calls == []


def test_cli_prefixes_each_violation_line_with_its_state(fake_git, capsys):
    fake_git.change('commit', 'app.py', 'x\n')
    fake_git.change('index', 'compose.yaml', 'x\n')

    assert boundary.main(['--base', BASE]) == 1

    assert capsys.readouterr().out == (
        'FAIL: core\n'
        '  index: unapproved Core path changed: compose.yaml\n'
        f'  {FIRST_COMMIT[:9]}: unapproved Core path changed: app.py\n'
        'PASS: catalogues\n'
    )


# fmt: off
@pytest.mark.parametrize(
    'path',
    [
        'app.py',
        'serve_web_apps.py',
        'compose.yaml',
        'babel.cfg',
        'docker/byceps/entrypoint.sh',
        'config/dev.toml',
        'scripts/tool.py',
        'assets/logo.svg',
        '.github/workflows/ci.yml',
        'docs/index.rst',
    ],
)
# fmt: on
def test_boundary_rejects_entrypoint_and_build_infrastructure_changes(
    fake_git, where, path
):
    fake_git.change(where, path, 'x = 1\n')

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: unapproved Core path changed: {path}'
    ]


# fmt: off
@pytest.mark.parametrize(
    'path',
    [
        'config/x.toml',
        'docker/byceps/extra.sh',
        'scripts/new_tool.py',
        '.github/workflows/new.yml',
        'assets/new.svg',
        'docs/new.rst',
        'SECURITY_AUDIT.md',
        'core',
        'docker',
        '.devcontainer/devcontainer.json',
        'brag-output/clip.mp4',
        '.kevins_workflow/plans/plan.md',
    ],
)
# fmt: on
def test_boundary_rejects_a_new_file_outside_the_exempt_set(
    fake_git, where, path
):
    fake_git.change(where, path, 'x\n')

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: unapproved Core path changed: {path}'
    ]


def test_boundary_lists_the_changes_of_each_state_without_a_pathspec(
    fake_git,
):
    fake_git.change('index', 'uv.lock', 'x\n')

    boundary.check_core(BASE)

    listings = [
        call
        for call in fake_git.calls
        if call[0] == 'diff' and '--name-only' in call and BASE_SHA in call
    ]
    assert all('--no-renames' in call for call in listings)
    assert {call[call.index(BASE_SHA) :] for call in listings} == {
        (BASE_SHA, '--'),
        (BASE_SHA, SECOND_COMMIT, '--'),
        (BASE_SHA, FIRST_COMMIT, '--'),
    }
    index_listings = [call for call in listings if '--cached' in call]
    assert [call[-2:] for call in index_listings] == [(BASE_SHA, '--')]


def test_boundary_never_reads_the_working_tree(fake_git):
    fake_git.change('index', 'uv.lock', 'x\n')

    boundary.check_core(BASE)
    boundary.check_catalogues(BASE)

    for call in fake_git.calls:
        if call[0] != 'diff':
            continue
        revisions = [
            arg for arg in call[1 : call.index('--')] if not arg.startswith('-')
        ]
        assert len(revisions) == (1 if '--cached' in call else 2), call
    assert {call[0] for call in fake_git.calls} <= READ_ONLY_COMMANDS


def test_boundary_content_diffs_ignore_attributes_and_filters(fake_git):
    fake_git.change('index', 'uv.lock', 'x\n')

    boundary.check_catalogues(BASE)

    content_calls = [
        call for call in fake_git.calls if call[0] == 'diff' and '-U0' in call
    ]
    assert len(content_calls) == 3
    for call in content_calls:
        assert set(call[call.index('--') + 1 :]) == {
            'Dockerfile',
            'justfile',
            'compose.yaml',
            'docker',
            'babel.cfg',
        }


def test_boundary_accepts_the_exact_loader_lines(fake_git, where):
    for path, added in LOADER_ADDITIONS.items():
        fake_git.set_loader(where, path, added=added)

    assert boundary.check_core(BASE) == []


def test_boundary_accepts_the_exact_legacy_loader_lines(fake_git, where):
    for path, added in LEGACY_LOADER_ADDITIONS.items():
        fake_git.set_loader(where, path, added=added)

    assert boundary.check_core(BASE) == []


def test_boundary_accepts_a_legacy_commit_below_a_current_tip(fake_git):
    for path, added in LEGACY_LOADER_ADDITIONS.items():
        fake_git.set_loader('commit', path, added=added)

    assert boundary.check_core(BASE) == []


def test_boundary_accepts_a_current_commit_below_a_legacy_tip(fake_git):
    for path, added in LEGACY_LOADER_ADDITIONS.items():
        fake_git.set_loader('head', path, added=added)

    assert boundary.check_core(BASE) == []


def test_boundary_accepts_a_legacy_head_below_a_current_index(fake_git):
    for path, added in LEGACY_LOADER_ADDITIONS.items():
        fake_git.set_loader('commit', path, added=added)
    fake_git.trees[SECOND_COMMIT] = dict(fake_git.trees[FIRST_COMMIT])

    assert boundary.check_core(BASE) == []


def test_boundary_accepts_one_changed_loader_from_either_set(fake_git, where):
    path = 'byceps/blueprints/site.py'
    for loader in LOADER_BASE:
        fake_git.set_loader(where, loader)
    fake_git.set_loader(where, path, added=LEGACY_LOADER_ADDITIONS[path])

    assert boundary.check_core(BASE) == []


# fmt: off
@pytest.mark.parametrize(
    'legacy',
    [
        ['byceps/application.py'],
        ['byceps/blueprints/admin.py'],
        ['byceps/blueprints/site.py'],
        ['byceps/application.py', 'byceps/blueprints/admin.py'],
        ['byceps/blueprints/admin.py', 'byceps/blueprints/site.py'],
    ],
    ids=['application', 'admin', 'site', 'application+admin', 'admin+site'],
)
# fmt: on
def test_boundary_rejects_loaders_that_mix_the_legacy_and_current_sets(
    fake_git, where, legacy
):
    for path in legacy:
        fake_git.set_loader(where, path, added=LEGACY_LOADER_ADDITIONS[path])

    violations = boundary.check_core(BASE)

    uses = ', '.join(
        f'{path} uses the {"legacy" if path in legacy else "current"} set'
        for path in sorted(LOADER_BASE)
    )
    message = f'loader lines mix the legacy and the current chair sets: {uses}'
    assert f'{LABELS[where]}: {message}' in violations
    assert len(violations) > 1


def test_boundary_names_the_loader_that_breaks_the_set_of_its_state(
    fake_git, where
):
    path = 'byceps/application.py'
    fake_git.set_loader(where, path, added=LEGACY_LOADER_ADDITIONS[path])

    assert boundary.check_core(BASE) == [
        (
            f'{LABELS[where]}: {path}: adds a non-chair line: '
            "'from byceps.services.chair_optout.lifecycle import "
            "enable_chair_lifecycle'"
        ),
        (
            f'{LABELS[where]}: {path}: lacks the chair loader line: '
            "'from byceps.services.chair_planning.lifecycle import "
            "enable_chair_lifecycle'"
        ),
        (
            f'{LABELS[where]}: loader lines mix the legacy and the current '
            f'chair sets: {path} uses the legacy set, '
            'byceps/blueprints/admin.py uses the current set, '
            'byceps/blueprints/site.py uses the current set'
        ),
    ]


def test_boundary_rejects_a_loader_that_mixes_both_sets_within_itself(
    fake_git, where
):
    # Alone in its state, the loader fits both sets equally: the current wins.
    path = 'byceps/blueprints/admin.py'
    legacy_line = LEGACY_LOADER_ADDITIONS[path][0]
    for loader in LOADER_BASE:
        fake_git.set_loader(where, loader)
    fake_git.set_loader(
        where,
        path,
        added=[legacy_line, *LOADER_ADDITIONS[path]],
    )

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: {path}: adds a non-chair line: {legacy_line!r}'
    ]


def test_boundary_judges_a_mix_in_one_state_only(fake_git):
    path = 'byceps/blueprints/site.py'
    fake_git.set_loader('commit', path, added=LEGACY_LOADER_ADDITIONS[path])

    violations = boundary.check_core(BASE)

    assert violations
    assert all(violation.startswith(FIRST_COMMIT[:9]) for violation in violations)


# fmt: off
@pytest.mark.parametrize(
    ('path', 'extra', 'message'),
    [
        ('byceps/application.py', [''], "adds a repeated loader line: ''"),
        ('byceps/blueprints/admin.py', [''], "adds a non-chair line: ''"),
        (
            'byceps/blueprints/site.py',
            ['import evil'],
            "adds a non-chair line: 'import evil'",
        ),
        (
            'byceps/blueprints/admin.py',
            LEGACY_LOADER_ADDITIONS['byceps/blueprints/admin.py'],
            (
                'adds a repeated loader line: '
                "\"        ('services.chair_optout.blueprints.admin', "
                "'/chair_optout'),\""
            ),
        ),
    ],
    ids=['blank-application', 'blank-admin', 'extra-site', 'repeated-admin'],
)
# fmt: on
def test_boundary_holds_the_legacy_lines_to_the_same_exactness(
    fake_git, where, path, extra, message
):
    for loader, added in LEGACY_LOADER_ADDITIONS.items():
        fake_git.set_loader(
            where, loader, added=[*added, *(extra if loader == path else [])]
        )

    assert boundary.check_core(BASE) == [f'{LABELS[where]}: {path}: {message}']


def test_boundary_rejects_a_legacy_loader_with_a_missing_chair_line(
    fake_git, where
):
    for loader, added in LEGACY_LOADER_ADDITIONS.items():
        fake_git.set_loader(
            where,
            loader,
            added=added[:2] if loader == 'byceps/application.py' else added,
        )

    assert boundary.check_core(BASE) == [
        (
            f"{LABELS[where]}: byceps/application.py: lacks the chair loader "
            "line: '    enable_chair_lifecycle()'"
        )
    ]


def test_boundary_rejects_a_legacy_loader_with_a_removed_line(fake_git, where):
    path = 'byceps/blueprints/site.py'
    removed = "        ('services.news.blueprints.site', '/news'),"
    for loader, added in LEGACY_LOADER_ADDITIONS.items():
        fake_git.set_loader(
            where,
            loader,
            removed=[removed] if loader == path else [],
            added=added,
        )

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: {path}: removes a line: {removed!r}'
    ]


# fmt: off
@pytest.mark.parametrize(
    'line',
    [
        'import subprocess  # chair_planning',
        'import subprocess  # chair_optout',
        'subprocess.run(["rm"])  # chair_lifecycle',
    ],
)
# fmt: on
def test_boundary_rejects_loader_line_that_only_mentions_the_chair(
    fake_git, where, line
):
    path = 'byceps/application.py'
    fake_git.set_loader(where, path, added=[*LOADER_ADDITIONS[path], line])

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: {path}: adds a non-chair line: {line!r}'
    ]


# fmt: off
@pytest.mark.parametrize(
    ('path', 'message'),
    [
        ('byceps/application.py', 'adds a repeated loader line'),
        ('byceps/blueprints/admin.py', 'adds a non-chair line'),
        ('byceps/blueprints/site.py', 'adds a non-chair line'),
    ],
)
# fmt: on
def test_boundary_rejects_loader_with_an_extra_blank_line(
    fake_git, where, path, message
):
    fake_git.set_loader(where, path, added=[*LOADER_ADDITIONS[path], ''])

    assert boundary.check_core(BASE) == [
        f"{LABELS[where]}: {path}: {message}: ''"
    ]


def test_boundary_rejects_loader_with_a_removed_line(fake_git, where):
    path = 'byceps/blueprints/site.py'
    removed = "        ('services.news.blueprints.site', '/news'),"
    fake_git.set_loader(
        where, path, removed=[removed], added=LOADER_ADDITIONS[path]
    )

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: {path}: removes a line: {removed!r}'
    ]


def test_boundary_rejects_loader_with_a_missing_chair_line(fake_git, where):
    path = 'byceps/application.py'
    fake_git.set_loader(where, path, added=LOADER_ADDITIONS[path][:2])

    assert boundary.check_core(BASE) == [
        (
            f"{LABELS[where]}: {path}: lacks the chair loader line: "
            "'    enable_chair_lifecycle()'"
        )
    ]


def test_boundary_rejects_loader_with_a_repeated_chair_line(fake_git, where):
    path = 'byceps/blueprints/admin.py'
    added = LOADER_ADDITIONS[path] * 2
    fake_git.set_loader(where, path, added=added)

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: {path}: adds a repeated loader line: {added[0]!r}'
    ]


def test_boundary_rejects_loader_with_a_modified_line(fake_git, where):
    path = 'byceps/blueprints/admin.py'
    removed = "        ('services.page.blueprints.admin', '/pages'),"
    modified = "        ('services.page.blueprints.admin', '/evil'),"
    fake_git.set_loader(
        where, path, removed=[removed], added=[*LOADER_ADDITIONS[path], modified]
    )

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: {path}: removes a line: {removed!r}',
        f'{LABELS[where]}: {path}: adds a non-chair line: {modified!r}',
    ]


def test_boundary_loader_comparison_is_exact_for_repeated_lines(
    fake_git, where
):
    path = 'byceps/application.py'
    import_line, blank, call = LOADER_ADDITIONS[path]
    base = []
    for number in range(70):
        base += [f'def f{number}():', '    pass', '']
    new = list(base)
    new.insert(0, import_line)
    new.insert(209, call)
    new.insert(209, blank)
    for tree in fake_git.trees.values():
        tree[path] = regular(join_lines(base))
    fake_git.change(where, path, join_lines(new))

    assert boundary.check_core(BASE) == []


def test_boundary_compares_loader_line_endings_exactly(fake_git, where):
    path = 'byceps/application.py'
    exact = join_lines([*LOADER_BASE[path], *LOADER_ADDITIONS[path]])
    fake_git.change(where, path, exact.replace('\n', '\r\n'))

    violations = boundary.check_core(BASE)

    assert (
        f"{LABELS[where]}: {path}: removes a line: "
        "'from byceps.database import db'"
    ) in violations


# fmt: off
@pytest.mark.parametrize('path', sorted(LOADER_BASE))
# fmt: on
def test_boundary_reports_a_deleted_loader(fake_git, where, path):
    fake_git.change(where, path, None)

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: {path}: is deleted'
    ]


def test_boundary_reports_a_loader_that_is_unmerged_in_the_index(fake_git):
    path = 'byceps/blueprints/admin.py'
    fake_git.change('index', 'uv.lock', 'x\n')
    fake_git.unmerged.add(path)

    assert boundary.check_core(BASE) == [
        f'index: {path}: has unmerged index entries'
    ]


# fmt: off
@pytest.mark.parametrize('stage', ['1', '2', '3'])
# fmt: on
def test_boundary_reports_a_loader_with_a_single_unmerged_stage(
    fake_git, stage
):
    path = 'byceps/blueprints/admin.py'
    fake_git.change('index', 'uv.lock', 'x\n')
    fake_git.entry_output[INDEX, path] = f'100644 {"a" * 40} {stage}\t{path}\n'

    assert boundary.check_core(BASE) == [
        f'index: {path}: has unmerged index entries'
    ]


def test_boundary_reports_a_readme_that_is_unmerged_in_the_index(fake_git):
    fake_git.change('index', 'uv.lock', 'x\n')
    fake_git.unmerged.add(boundary.README_PATH)

    assert boundary.check_catalogues(BASE) == [
        f'index: {boundary.README_PATH}: has unmerged index entries'
    ]


def test_boundary_reports_a_legacy_readme_that_is_unmerged_in_the_index(
    fake_git,
):
    fake_git.change('index', boundary.LEGACY_README_PATH, CLEAN_README)
    fake_git.unmerged.add(boundary.LEGACY_README_PATH)

    assert boundary.check_catalogues(BASE) == [
        f'index: {boundary.LEGACY_README_PATH}: has unmerged index entries'
    ]


def test_boundary_reports_a_loader_missing_at_base(fake_git):
    path = 'byceps/blueprints/admin.py'
    del fake_git.trees[BASE_SHA][path]

    assert boundary.check_core(BASE) == [f'{path} is missing at base']


def test_boundary_reports_a_loader_replaced_by_a_directory(fake_git):
    path = 'byceps/blueprints/admin.py'
    for place in ('commit', 'index'):
        fake_git.change(place, path, None)
        fake_git.change(place, f'{path}/evil.py', 'x\n')

    assert boundary.check_core(BASE) == [
        f'index: {path}: is deleted',
        f'index: unapproved Core path changed: {path}/evil.py',
        f'{FIRST_COMMIT[:9]}: {path}: file mode changed from 100644 to 040000',
        f'{FIRST_COMMIT[:9]}: unapproved Core path changed: {path}/evil.py',
    ]


# fmt: off
@pytest.mark.parametrize('path', sorted(LOADER_BASE))
@pytest.mark.parametrize(
    'new_mode',
    ['100755', '120000', '160000'],
    ids=['exec', 'symlink', 'gitlink'],
)
# fmt: on
def test_boundary_rejects_loader_with_a_changed_git_mode(
    fake_git, where, path, new_mode
):
    exact = fake_git.trees[INDEX][path].content
    fake_git.change(where, path, exact, mode=new_mode)

    assert boundary.check_core(BASE) == [
        (
            f'{LABELS[where]}: {path}: file mode changed from 100644 '
            f'to {new_mode}'
        )
    ]


def test_boundary_accepts_a_loader_that_keeps_its_base_mode(fake_git):
    for tree in fake_git.trees.values():
        for path in LOADER_BASE:
            tree[path] = tree[path]._replace(mode='100755')

    assert boundary.check_core(BASE) == []


def test_boundary_reports_loader_mode_and_content_together(fake_git, where):
    path = 'byceps/blueprints/site.py'
    removed = "        ('services.news.blueprints.site', '/news'),"
    fake_git.set_loader(
        where, path, removed=[removed], added=LOADER_ADDITIONS[path]
    )
    for key in WHERE_TREES[where]:
        fake_git.trees[key][path] = fake_git.trees[key][path]._replace(
            mode='100755'
        )

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: {path}: file mode changed from 100644 to 100755',
        f'{LABELS[where]}: {path}: removes a line: {removed!r}',
    ]


# fmt: off
@pytest.mark.parametrize(
    'raw',
    ['garbage', '100644 abc\tbyceps/application.py'],
    ids=['no-fields', 'short-meta'],
)
# fmt: on
def test_boundary_fails_when_git_lists_no_usable_entry(fake_git, where, raw):
    fake_git.change('index', 'uv.lock', 'x\n')
    key = INDEX if where == 'index' else FIRST_COMMIT
    fake_git.entry_output[key, 'byceps/application.py'] = raw + '\n'

    command = 'ls-files' if where == 'index' else 'ls-tree'
    assert boundary.check_core(BASE) == [
        f'unexpected output from git {command}'
    ]


def test_boundary_asks_git_for_the_entry_of_each_loader_alone(fake_git):
    fake_git.change('index', 'uv.lock', 'x\n')

    boundary.check_core(BASE)

    index_calls = [call for call in fake_git.calls if call[0] == 'ls-files']
    assert all(call[:2] == ('ls-files', '-s') for call in index_calls)
    assert all(call[2] == '--' for call in index_calls)
    assert sorted(call[-1] for call in index_calls) == sorted(LOADER_BASE)
    tree_calls = [call for call in fake_git.calls if call[0] == 'ls-tree']
    # Each state asks for its own entry, and for the base entry to compare.
    assert Counter((call[1], call[-1]) for call in tree_calls) == Counter(
        {
            (sha, path): count
            for path in LOADER_BASE
            for sha, count in (
                (BASE_SHA, 3),
                (SECOND_COMMIT, 1),
                (FIRST_COMMIT, 1),
            )
        }
    )
    assert all(call[2] == '--' for call in tree_calls)


def test_boundary_diffs_never_ignore_submodules(fake_git):
    fake_git.change('index', 'uv.lock', 'x\n')

    boundary.check_core(BASE)
    boundary.check_catalogues(BASE)

    diffs = [c for c in fake_git.calls if c[0] in ('diff', 'diff-tree')]
    assert {flag for call in diffs for flag in call} >= {
        '--name-only',
        '-U0',
        '--cached',
    }
    assert 'diff-tree' in {call[0] for call in diffs}
    assert all('--ignore-submodules=none' in call for call in diffs)


# fmt: off
@pytest.mark.parametrize(
    'path',
    [
        '.gitattributes',
        'byceps/blueprints/.gitattributes',
        'tests/unit/.gitattributes',
        'sites/totalverplant/.gitattributes',
        'byceps/services/chair_planning/.gitattributes',
        'byceps/services/chair_optout/.gitattributes',
        'docs/guide/.gitattributes',
        'not_in_base/.gitattributes',
    ],
)
# fmt: on
def test_boundary_rejects_gitattributes_anywhere(fake_git, where, path):
    fake_git.change(where, path, '* -diff\n')

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: unapproved Core path changed: {path}'
    ]


# fmt: off
@pytest.mark.parametrize(
    'path',
    [
        '.gitmodules',
        'byceps/blueprints/.gitmodules',
        'tests/unit/.gitmodules',
        'sites/totalverplant/.gitmodules',
        'byceps/services/chair_planning/.gitmodules',
        'byceps/services/chair_optout/.gitmodules',
        'docs/guide/.gitmodules',
        'not_in_base/.gitmodules',
    ],
)
# fmt: on
def test_boundary_rejects_gitmodules_anywhere(fake_git, where, path):
    fake_git.change(where, path, 'ignore = all\n')

    assert boundary.check_core(BASE) == [
        f'{LABELS[where]}: unapproved Core path changed: {path}'
    ]


def test_boundary_reports_the_index_once_when_it_equals_head(fake_git):
    fake_git.change('head', 'Dockerfile', 'FROM scratch\n')

    assert boundary.check_core(BASE) == [
        'index/HEAD: build file changed: Dockerfile (must match base)'
    ]


def test_boundary_reports_the_index_and_head_when_they_differ(fake_git):
    fake_git.change('head', 'app.py', 'x\n')
    fake_git.change('index', 'Dockerfile', 'FROM scratch\n')

    assert boundary.check_core(BASE) == [
        'index: build file changed: Dockerfile (must match base)',
        'index: unapproved Core path changed: app.py',
        f'{SECOND_COMMIT[:9]}: unapproved Core path changed: app.py',
    ]


def test_boundary_judges_the_index_when_the_branch_has_no_commits(fake_git):
    fake_git.log = []
    fake_git.head = BASE_SHA
    fake_git.change('index', 'app.py', 'x\n')

    assert boundary.check_core(BASE) == [
        'index: unapproved Core path changed: app.py'
    ]
    assert boundary.check_catalogues(BASE) == []


def test_boundary_judges_a_commit_that_a_later_commit_reverts(fake_git):
    fake_git.change('commit', 'Dockerfile', 'FROM scratch\n')

    assert fake_git.trees[INDEX] == fake_git.trees[SECOND_COMMIT]
    assert boundary.check_core(BASE) == [
        (
            f'{FIRST_COMMIT[:9]}: build file changed: Dockerfile '
            '(must match base)'
        )
    ]


# fmt: off
@pytest.mark.parametrize(
    'separator',
    ['\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\x85', ' ', ' '],
    ids=['cr', 'vt', 'ff', 'fs', 'gs', 'rs', 'nel', 'ls', 'ps'],
)
# fmt: on
def test_boundary_compile_scan_reads_past_a_line_separator(
    fake_git, where, separator
):
    line = f'x{separator}command: pybabel compile'
    fake_git.add_lines(where, 'compose.yaml', [line])

    assert boundary.check_catalogues(BASE) == [
        f'{LABELS[where]}: build file adds catalogue compile step: {line!r}'
    ]


# fmt: off
@pytest.mark.parametrize(
    'final_newline', [True, False], ids=['final-newline', 'no-final-newline']
)
# fmt: on
def test_boundary_compile_scan_ignores_the_final_newline(
    fake_git, where, final_newline
):
    for tree in fake_git.trees.values():
        tree['Dockerfile'] = regular('FROM python:3.12\r\n')
    fake_git.change(
        where,
        'Dockerfile',
        'FROM python:3.13\r\nRUN pybabel compile\r\n',
    )
    fake_git.strip_final_newline = not final_newline

    assert boundary.check_catalogues(BASE) == [
        (
            f'{LABELS[where]}: build file adds catalogue compile step: '
            "'RUN pybabel compile'"
        )
    ]


def test_run_git_returns_raw_output(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, stdout=b'a\r\nb\xff\n', stderr=b''
        )

    monkeypatch.setattr(boundary.subprocess, 'run', fake_run)

    assert boundary.run_git('cat-file', 'blob', 'x:y') == 'a\r\nb\udcff\n'


def test_boundary_has_no_working_tree_readers():
    assert not hasattr(boundary, 'read_working_file')
    assert not hasattr(boundary, 'lstat_working_file')


REAL_FILES = {
    'app.py': "print('entrypoint')\n",
    'compose.yaml': 'services: {}\n',
    'docker/byceps/entrypoint.sh': '#!/bin/sh\nexec "$@"\n',
    'config/base.toml': 'a = 1\n',
    'tests/test_base.py': 'x = 1\n',
    'sites/s/logo.svg': '<svg/>\n',
    'Dockerfile': 'FROM python:3.13\n',
    'byceps/static/x.css': 'a { color: red; }\n',
    PO: 'msgid "a"\nmsgstr "b"\n',
    MO: 'compiled\n',
    'byceps/application.py': (
        'from byceps.database import db\n'
        '\n'
        '\n'
        'def create(app):\n'
        '    db.init_app(app)\n'
        '\n'
        '    app.run()\n'
    ),
    'byceps/blueprints/admin.py': (
        '    blueprints = [\n'
        "        ('services.page.blueprints.admin', '/pages'),\n"
        "        ('services.seating.blueprints.admin', '/seating'),\n"
        '    ]\n'
    ),
    'byceps/blueprints/site.py': (
        '    blueprints = [\n'
        "        ('services.news.blueprints.site', '/news'),\n"
        "        ('services.seating.blueprints.site', '/seating'),\n"
        '    ]\n'
    ),
}
GITMODULES = (
    '[submodule "evilsub"]\n'
    '\tpath = byceps/services/evilsub\n'
    '\turl = https://example.invalid/evil.git\n'
    '\tignore = all\n'
    '[submodule "catalogues"]\n'
    '\tpath = byceps/translations/de/sub\n'
    '\turl = https://example.invalid/catalogues.git\n'
    '\tignore = all\n'
)
GIT_IDENTITY = (
    '-c',
    'user.name=Test',
    '-c',
    'user.email=test@example.invalid',
    '-c',
    'commit.gpgsign=false',
)
LOADERS = sorted(LOADER_BASE)


def git(root, *args):
    result = subprocess.run(  # noqa: S603 - fixed git argv, throwaway repo.
        ['git', *GIT_IDENTITY, *args],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
    )
    return result.stdout.decode()


def init_repo(root, monkeypatch, files):
    """Commit `files` as `main` in a hermetic repository, then branch."""
    for name, value in {
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_NOSYSTEM': '1',
        'XDG_CONFIG_HOME': str(root.parent / 'xdg'),
    }.items():
        monkeypatch.setenv(name, value)
    for name in ('GIT_DIR', 'GIT_INDEX_FILE', 'GIT_WORK_TREE'):
        monkeypatch.delenv(name, raising=False)

    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'commit.gpgsign', 'false')
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', 'base')
    git(root, 'checkout', '-q', '-b', 'chair')

    monkeypatch.chdir(root)
    monkeypatch.setattr(boundary, 'REPO_ROOT', root)
    return root


def add_chair_loaders(root, additions=LOADER_ADDITIONS):
    import_line, _, call = additions['byceps/application.py']
    application = root / 'byceps/application.py'
    application.write_text(
        application.read_text()
        .replace(
            'from byceps.database import db\n',
            f'from byceps.database import db\n{import_line}\n',
        )
        .replace(
            '    db.init_app(app)\n\n',
            f'    db.init_app(app)\n\n{call}\n\n',
        )
    )

    for kind in ('admin', 'site'):
        path = root / f'byceps/blueprints/{kind}.py'
        (line,) = additions[f'byceps/blueprints/{kind}.py']
        anchor = f"        ('services.seating.blueprints.{kind}', '/seating'),\n"
        path.write_text(path.read_text().replace(anchor, f'{line}\n{anchor}'))


def commit_all(root, message='change'):
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', message)
    return git(root, 'rev-parse', 'HEAD').strip()


def commit_feature(root, *, legacy=False):
    """Commit what a chair feature may add: loaders, `.po`, chair files.

    With `legacy`, write it the way the history before the rename did.
    """
    if legacy:
        additions, prefix = LEGACY_LOADER_ADDITIONS, LEGACY_PREFIX
        readme = boundary.LEGACY_README_PATH
    else:
        additions, prefix = LOADER_ADDITIONS, PREFIX
        readme = boundary.README_PATH

    add_chair_loaders(root, additions)
    po = root / PO
    po.write_text(po.read_text() + 'msgid "chair"\nmsgstr "Stuhl"\n')
    for name, content in {
        f'{prefix}lifecycle.py': 'x\n',
        readme: CLEAN_README,
        'tests/unit/test_chair.py': 'x\n',
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return commit_all(root, 'feature')


def rename_to_chair_planning(root):
    """Move the legacy module to its new package and swap the loader lines."""
    git(root, 'mv', LEGACY_PREFIX.rstrip('/'), PREFIX.rstrip('/'))
    for name, legacy_lines in LEGACY_LOADER_ADDITIONS.items():
        path = root / name
        text = path.read_text()
        for old, new in zip(legacy_lines, LOADER_ADDITIONS[name], strict=True):
            if old != new:
                text = text.replace(f'{old}\n', f'{new}\n')
        path.write_text(text)


def add_embedded_repo(root, name):
    """Stage `name` as a gitlink to a populated nested repository."""
    nested = root / name
    nested.mkdir(parents=True)
    git(nested, 'init', '-q', '-b', 'main')
    (nested / 'f').write_text('x\n')
    git(nested, 'add', 'f')
    git(nested, 'commit', '-q', '-m', 'nested')
    git(root, 'add', name)


def stage_or_commit(root, how):
    """Record the working tree changes in the index, and maybe a commit."""
    if how == 'committed':
        commit_all(root)
    else:
        git(root, 'add', '-A')


def run_cli(capsys, *args):
    code = boundary.main(['--base', 'main', *args])
    return code, capsys.readouterr().out


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return init_repo(tmp_path / 'repo', monkeypatch, REAL_FILES)


@pytest.fixture
def feature_repo(repo):
    commit_feature(repo)
    return repo


@pytest.fixture
def submodule_repo(tmp_path, monkeypatch):
    files = {**REAL_FILES, '.gitmodules': GITMODULES}
    return init_repo(tmp_path / 'repo', monkeypatch, files)


@pytest.fixture(
    params=[
        'byceps/application.py -diff\nDockerfile -diff\n',
        '*.py binary\nDockerfile binary\n',
    ],
    ids=['diff-off', 'binary'],
)
def attributed_repo(request, tmp_path, monkeypatch):
    files = {**REAL_FILES, '.gitattributes': request.param}
    return init_repo(tmp_path / 'repo', monkeypatch, files)


@pytest.fixture(params=['staged', 'committed'])
def how(request):
    return request.param


LABEL_FOR = {'staged': 'index', 'committed': 'index/HEAD'}


def test_real_git_accepts_a_feature_commit_with_exact_loaders_and_po(
    repo, capsys
):
    commit_feature(repo)

    assert run_cli(capsys) == (0, 'PASS: core\nPASS: catalogues\n')


def test_real_git_accepts_the_staged_chair_additions(repo, capsys):
    add_chair_loaders(repo)
    git(repo, 'add', '-A')

    assert run_cli(capsys) == (0, 'PASS: core\nPASS: catalogues\n')


def test_real_git_accepts_the_staged_legacy_chair_additions(repo, capsys):
    add_chair_loaders(repo, LEGACY_LOADER_ADDITIONS)
    git(repo, 'add', '-A')

    assert run_cli(capsys) == (0, 'PASS: core\nPASS: catalogues\n')


def test_real_git_accepts_a_legacy_feature_commit(repo, capsys):
    commit_feature(repo, legacy=True)

    assert run_cli(capsys) == (0, 'PASS: core\nPASS: catalogues\n')


def test_real_git_accepts_a_legacy_commit_followed_by_the_rename(
    repo, capsys, how
):
    commit_feature(repo, legacy=True)
    rename_to_chair_planning(repo)
    stage_or_commit(repo, how)

    assert run_cli(capsys) == (0, 'PASS: core\nPASS: catalogues\n')
    assert (repo / boundary.README_PATH).is_file()
    assert not (repo / boundary.LEGACY_README_PATH).exists()


def test_real_git_rejects_loaders_that_mix_the_legacy_and_current_sets(
    repo, capsys, how
):
    add_chair_loaders(repo)
    application = repo / 'byceps/application.py'
    legacy_import, current_import = (
        additions['byceps/application.py'][0]
        for additions in (LEGACY_LOADER_ADDITIONS, LOADER_ADDITIONS)
    )
    application.write_text(
        application.read_text().replace(current_import, legacy_import)
    )
    stage_or_commit(repo, how)

    label = LABEL_FOR[how]
    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            f'  {label}: byceps/application.py: '
            f'adds a non-chair line: {legacy_import!r}\n'
            f'  {label}: byceps/application.py: '
            f'lacks the chair loader line: {current_import!r}\n'
            f'  {label}: loader lines mix the legacy and the current chair '
            'sets: byceps/application.py uses the legacy set, '
            'byceps/blueprints/admin.py uses the current set, '
            'byceps/blueprints/site.py uses the current set\n'
            'PASS: catalogues\n'
        ),
    )


def test_real_git_rejects_a_mixed_rename_that_swaps_only_the_registrars(
    repo, capsys, how
):
    commit_feature(repo, legacy=True)
    rename_to_chair_planning(repo)
    application = repo / 'byceps/application.py'
    current_import = LOADER_ADDITIONS['byceps/application.py'][0]
    legacy_import = LEGACY_LOADER_ADDITIONS['byceps/application.py'][0]
    application.write_text(
        application.read_text().replace(current_import, legacy_import)
    )
    stage_or_commit(repo, how)

    code, out = run_cli(capsys)

    assert code == 1
    assert out.startswith('FAIL: core\n')
    assert (
        f'  {LABEL_FOR[how]}: loader lines mix the legacy and the current '
        'chair sets: byceps/application.py uses the legacy set'
    ) in out


# fmt: off
@pytest.mark.parametrize(
    ('name', 'content', 'message'),
    [
        (
            'byceps/services/ticketing/new_helper.py',
            'x = 1\n',
            (
                'unapproved Core path changed: '
                'byceps/services/ticketing/new_helper.py'
            ),
        ),
        (
            'Dockerfile',
            'FROM scratch\n',
            'build file changed: Dockerfile (must match base)',
        ),
        (
            'byceps/static/x.css',
            'a { color: blue; }\n',
            (
                'Core static asset changed: byceps/static/x.css '
                '(must match base)'
            ),
        ),
    ],
    ids=['new-file', 'build-file', 'static-asset'],
)
# fmt: on
def test_real_git_rejects_an_unapproved_core_path_inside_the_rename(
    repo, capsys, how, name, content, message
):
    commit_feature(repo, legacy=True)
    rename_to_chair_planning(repo)
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    stage_or_commit(repo, how)

    assert run_cli(capsys) == (
        1,
        f'FAIL: core\n  {LABEL_FOR[how]}: {message}\nPASS: catalogues\n',
    )


def test_real_git_judges_the_rename_not_the_legacy_commit_below_it(
    repo, capsys
):
    commit_feature(repo, legacy=True)
    rename_to_chair_planning(repo)
    commit_all(repo, 'rename')
    (repo / 'Dockerfile').write_text('FROM scratch\n')
    git(repo, 'add', 'Dockerfile')

    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            '  index: build file changed: Dockerfile (must match base)\n'
            'PASS: catalogues\n'
        ),
    )


def test_real_git_scans_both_readmes_when_both_exist(repo, capsys, how):
    commit_feature(repo, legacy=True)
    rename_to_chair_planning(repo)
    legacy_readme = repo / boundary.LEGACY_README_PATH
    legacy_readme.parent.mkdir(parents=True, exist_ok=True)
    legacy_readme.write_text('Run `just babel-compile`.\n')
    current_readme = repo / boundary.README_PATH
    current_readme.write_text('Setup\nRun `just babel-compile`.\n')
    stage_or_commit(repo, how)

    label = LABEL_FOR[how]
    assert run_cli(capsys, '--check', 'catalogues') == (
        1,
        (
            'FAIL: catalogues\n'
            f'  {label}: {boundary.LEGACY_README_PATH}:1: '
            'local catalogue compile guidance\n'
            f'  {label}: {boundary.README_PATH}:2: '
            'local catalogue compile guidance\n'
        ),
    )


def test_real_git_ignores_unstaged_and_untracked_changes(feature_repo, capsys):
    repo = feature_repo
    application = repo / 'byceps/application.py'
    application.write_text(application.read_text() + 'import evil\n')
    (repo / 'byceps/blueprints/admin.py').chmod(0o755)
    (repo / 'byceps/blueprints/site.py').unlink()
    for name in ('app.py', 'Dockerfile', 'compose.yaml'):
        path = repo / name
        path.write_text(path.read_text() + 'RUN pybabel compile -d x\n')
    (repo / MO).write_text('compiled-local\n')
    readme = repo / boundary.README_PATH
    readme.write_text('Run `just babel-compile` after pulling.\n')
    for name in (
        'config/new.toml',
        'byceps/static/new.css',
        'byceps/services/ticketing/new_helper.py',
        'SECURITY_AUDIT.md',
        '.gitattributes',
        '.gitmodules',
        'byceps/translations/de/LC_MESSAGES/extra.mo',
    ):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x\n')

    assert run_cli(capsys) == (0, 'PASS: core\nPASS: catalogues\n')


# fmt: off
@pytest.mark.parametrize(
    'name',
    ['app.py', 'compose.yaml', 'docker/byceps/entrypoint.sh'],
)
# fmt: on
def test_real_git_rejects_a_staged_core_edit(feature_repo, capsys, name):
    path = feature_repo / name
    path.write_text(path.read_text() + '# edit\n')
    git(feature_repo, 'add', name)

    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            f'  index: unapproved Core path changed: {name}\n'
            'PASS: catalogues\n'
        ),
    )


def test_real_git_judges_the_index_not_the_working_file(feature_repo, capsys):
    path = feature_repo / 'app.py'
    original = path.read_bytes()
    path.write_bytes(original + b'# edit\n')
    git(feature_repo, 'add', 'app.py')
    path.write_bytes(original)

    assert git(feature_repo, 'status', '--porcelain') == 'MM app.py\n'
    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            '  index: unapproved Core path changed: app.py\n'
            'PASS: catalogues\n'
        ),
    )


def test_real_git_rejects_a_staged_loader_change(feature_repo, capsys):
    application = feature_repo / 'byceps/application.py'
    application.write_text(application.read_text() + 'import evil\n')
    git(feature_repo, 'add', 'byceps/application.py')

    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            "  index: byceps/application.py: adds a non-chair line: "
            "'import evil'\n"
            'PASS: catalogues\n'
        ),
    )


def test_real_git_judges_head_when_the_index_fixes_it(repo, capsys):
    add_chair_loaders(repo)
    dockerfile = repo / 'Dockerfile'
    dockerfile.write_text('FROM python:3.14\n')
    head = commit_all(repo)
    dockerfile.write_text('FROM python:3.13\n')
    git(repo, 'add', 'Dockerfile')

    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            f'  {head[:9]}: build file changed: Dockerfile (must match base)\n'
            'PASS: catalogues\n'
        ),
    )


def test_real_git_rejects_a_core_edit_reverted_by_a_later_commit(
    feature_repo, capsys
):
    path = feature_repo / 'app.py'
    original = path.read_text()
    path.write_text(original + '# edit\n')
    intermediate = commit_all(feature_repo, 'edit')
    path.write_text(original)
    commit_all(feature_repo, 'revert')

    changed = git(feature_repo, 'diff', '--name-only', 'main', 'HEAD')
    assert 'app.py' not in changed.split()
    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            f'  {intermediate[:9]}: unapproved Core path changed: app.py\n'
            'PASS: catalogues\n'
        ),
    )


# fmt: off
@pytest.mark.parametrize(
    ('name', 'outside'),
    [
        ('byceps/application.py', False),
        ('byceps/blueprints/admin.py', False),
        ('byceps/blueprints/site.py', True),
    ],
    ids=['application-chair-dir', 'admin-chair-dir', 'site-outside-repo'],
)
# fmt: on
def test_real_git_rejects_loader_replaced_by_a_symlink(
    feature_repo, tmp_path, capsys, name, outside, how
):
    loader = feature_repo / name
    if outside:
        destination = tmp_path / 'outside_loader.py'
        target = str(destination)
    else:
        destination = feature_repo / 'byceps/services/chair_planning/copy.py'
        target = os.path.relpath(destination, loader.parent)
    destination.write_bytes(loader.read_bytes())
    loader.unlink()
    loader.symlink_to(target)
    stage_or_commit(feature_repo, how)
    recorded = (
        git(feature_repo, 'ls-tree', 'HEAD', name)
        if how == 'committed'
        else git(feature_repo, 'ls-files', '-s', name)
    )
    assert recorded.startswith('120000')

    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            f'  {LABEL_FOR[how]}: {name}: '
            'file mode changed from 100644 to 120000\n'
            'PASS: catalogues\n'
        ),
    )


# fmt: off
@pytest.mark.parametrize('name', LOADERS)
# fmt: on
def test_real_git_rejects_loader_with_the_exec_bit(
    feature_repo, capsys, name, how
):
    (feature_repo / name).chmod(0o755)
    stage_or_commit(feature_repo, how)

    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            f'  {LABEL_FOR[how]}: {name}: '
            'file mode changed from 100644 to 100755\n'
            'PASS: catalogues\n'
        ),
    )


# fmt: off
@pytest.mark.parametrize('how', ['unstaged', 'staged', 'committed'])
# fmt: on
def test_real_git_reports_a_deleted_loader_without_a_traceback(
    feature_repo, capsys, how
):
    name = 'byceps/blueprints/site.py'
    (feature_repo / name).unlink()
    if how != 'unstaged':
        stage_or_commit(feature_repo, how)

    expected = (
        (0, 'PASS: core\nPASS: catalogues\n')
        if how == 'unstaged'
        else (
            1,
            (
                'FAIL: core\n'
                f'  {LABEL_FOR[how]}: {name}: is deleted\n'
                'PASS: catalogues\n'
            ),
        )
    )
    assert run_cli(capsys) == expected


def test_real_git_rejects_loader_replaced_by_a_directory(
    feature_repo, capsys, how
):
    name = 'byceps/blueprints/site.py'
    (feature_repo / name).unlink()
    (feature_repo / name).mkdir()
    (feature_repo / name / 'evil.py').write_text('x\n')
    stage_or_commit(feature_repo, how)

    problem = (
        f'{name}: file mode changed from 100644 to 040000'
        if how == 'committed'
        else f'{name}: is deleted'
    )
    label = LABEL_FOR[how]
    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            f'  {label}: {problem}\n'
            f'  {label}: unapproved Core path changed: {name}/evil.py\n'
            'PASS: catalogues\n'
        ),
    )


def test_real_git_compares_committed_loader_line_endings_exactly(
    feature_repo, capsys, how
):
    application = feature_repo / 'byceps/application.py'
    application.write_bytes(application.read_bytes().replace(b'\n', b'\r\n'))
    stage_or_commit(feature_repo, how)

    code, output = run_cli(capsys)

    assert code == 1
    assert (
        f"  {LABEL_FOR[how]}: byceps/application.py: removes a line: "
        "'from byceps.database import db'\n"
    ) in output


def test_real_git_rejects_committed_attributes_and_submodules(
    feature_repo, capsys, how
):
    (feature_repo / '.gitattributes').write_text('* -diff\n')
    (feature_repo / '.gitmodules').write_text(GITMODULES)
    stage_or_commit(feature_repo, how)

    label = LABEL_FOR[how]
    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            f'  {label}: unapproved Core path changed: .gitattributes\n'
            f'  {label}: unapproved Core path changed: .gitmodules\n'
            'PASS: catalogues\n'
        ),
    )


# fmt: off
@pytest.mark.parametrize(
    'attributes',
    ['byceps/application.py -diff\n', '*.py binary\n'],
    ids=['diff-off', 'binary'],
)
# fmt: on
def test_real_git_rejects_attribute_hidden_loader_change(
    repo, capsys, attributes, how
):
    add_chair_loaders(repo)
    application = repo / 'byceps/application.py'
    application.write_text(
        application.read_text().replace(
            '    app.run()\n', '    app.run(debug=True)\n'
        )
        + 'import evil\n'
    )
    (repo / '.gitattributes').write_text(attributes)
    stage_or_commit(repo, how)

    code, output = run_cli(capsys)

    assert code == 1
    label = LABEL_FOR[how]
    assert f'  {label}: unapproved Core path changed: .gitattributes\n' in output
    for violation in (
        "removes a line: '    app.run()'",
        "adds a non-chair line: 'import evil'",
    ):
        assert f'  {label}: byceps/application.py: {violation}\n' in output


# fmt: off
@pytest.mark.parametrize(
    'line',
    [
        'import subprocess  # chair_planning',
        'import subprocess  # chair_optout',
        'subprocess.run(["rm"])  # chair_lifecycle',
    ],
)
# fmt: on
def test_real_git_rejects_loader_line_that_only_mentions_the_chair(
    feature_repo, capsys, how, line
):
    application = feature_repo / 'byceps/application.py'
    application.write_text(application.read_text() + f'{line}\n')
    stage_or_commit(feature_repo, how)

    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            f'  {LABEL_FOR[how]}: byceps/application.py: '
            f'adds a non-chair line: {line!r}\n'
            'PASS: catalogues\n'
        ),
    )


def test_real_git_loader_check_ignores_attributes_present_at_base(
    attributed_repo, capsys
):
    add_chair_loaders(attributed_repo)
    application = attributed_repo / 'byceps/application.py'
    application.write_text(
        application.read_text().replace(
            '    app.run()\n', '    app.run(debug=True)\n'
        )
        + 'import evil\n'
    )
    commit_all(attributed_repo)

    code, output = run_cli(capsys)

    assert code == 1
    assert '.gitattributes' not in output
    for violation in (
        "removes a line: '    app.run()'",
        "adds a non-chair line: '    app.run(debug=True)'",
        "adds a non-chair line: 'import evil'",
    ):
        assert f'  index/HEAD: byceps/application.py: {violation}\n' in output


def test_real_git_compile_scan_ignores_diff_attributes(attributed_repo):
    dockerfile = attributed_repo / 'Dockerfile'
    dockerfile.write_text(
        dockerfile.read_text() + 'RUN pybabel compile -d byceps/translations\n'
    )
    commit_all(attributed_repo)

    assert boundary.check_catalogues('main') == [
        (
            "index/HEAD: build file adds catalogue compile step: "
            "'RUN pybabel compile -d byceps/translations'"
        )
    ]


def test_real_git_rejects_compile_step_in_a_build_file(feature_repo, how):
    path = feature_repo / 'docker/byceps/entrypoint.sh'
    path.write_text(
        path.read_text() + 'pybabel compile -d byceps/translations\n'
    )
    stage_or_commit(feature_repo, how)

    assert boundary.check_catalogues('main') == [
        (
            f"{LABEL_FOR[how]}: build file adds catalogue compile step: "
            "'pybabel compile -d byceps/translations'"
        )
    ]


# fmt: off
@pytest.mark.parametrize(
    'separator', ['\r', '\x0c', ' '], ids=['cr', 'ff', 'ls']
)
@pytest.mark.parametrize('name', ['compose.yaml', 'docker/byceps/entrypoint.sh'])
# fmt: on
def test_real_git_compile_scan_reads_past_a_line_separator(
    feature_repo, capsys, name, separator, how
):
    line = f'x{separator}command: pybabel compile'
    path = feature_repo / name
    path.write_bytes(path.read_bytes() + f'{line}\n'.encode())
    stage_or_commit(feature_repo, how)

    assert run_cli(capsys, '--check', 'catalogues') == (
        1,
        (
            'FAIL: catalogues\n'
            f'  {LABEL_FOR[how]}: build file adds catalogue compile step: '
            f'{line!r}\n'
        ),
    )


# fmt: off
@pytest.mark.parametrize('how', ['unstaged', 'staged', 'committed'])
# fmt: on
def test_real_git_readme_compile_advice_is_judged_when_staged_or_committed(
    feature_repo, capsys, how
):
    readme = feature_repo / boundary.README_PATH
    readme.write_text('Setup\nRun `just babel-compile` after pulling.\n')
    if how != 'unstaged':
        stage_or_commit(feature_repo, how)

    expected = (
        (0, 'PASS: catalogues\n')
        if how == 'unstaged'
        else (
            1,
            (
                'FAIL: catalogues\n'
                f'  {LABEL_FOR[how]}: {boundary.README_PATH}:2: '
                'local catalogue compile guidance\n'
            ),
        )
    )
    assert run_cli(capsys, '--check', 'catalogues') == expected


def test_real_git_readme_symlink_is_not_scanned_through(feature_repo, how):
    readme = feature_repo / boundary.README_PATH
    notes = readme.parent / 'notes.md'
    notes.write_text('Run `just babel-compile`.\n')
    readme.unlink()
    readme.symlink_to('notes.md')
    stage_or_commit(feature_repo, how)

    assert boundary.check_catalogues('main') == [
        f'{LABEL_FOR[how]}: {boundary.README_PATH}: is not a regular file'
    ]


def test_real_git_rejects_intermediate_mo_commit_even_if_head_is_clean(
    feature_repo, capsys
):
    mo = feature_repo / MO
    mo.write_text('compiled-local\n')
    intermediate = commit_all(feature_repo, 'compile')
    mo.write_text('compiled\n')
    tip = commit_all(feature_repo, 'restore')

    changed = git(feature_repo, 'diff', '--name-only', 'main', 'HEAD')
    assert MO not in changed.split()
    assert run_cli(capsys) == (
        1,
        (
            'PASS: core\n'
            'FAIL: catalogues\n'
            f'  {tip[:9]}: touched generated catalogue {MO}\n'
            f'  {intermediate[:9]}: touched generated catalogue {MO}\n'
        ),
    )


def test_real_git_rejects_a_stale_catalogue_when_staged_or_committed(
    feature_repo, how
):
    (feature_repo / MO).write_text('compiled-local\n')
    stage_or_commit(feature_repo, how)

    expected = [
        f'{LABEL_FOR[how]}: stale generated catalogue: {MO} differs from base'
    ]
    if how == 'committed':
        head = git(feature_repo, 'rev-parse', 'HEAD').strip()
        expected.insert(0, f'{head[:9]}: touched generated catalogue {MO}')
    assert boundary.check_catalogues('main') == expected


def test_real_git_rejects_gitlink_with_a_gitmodules_file(
    feature_repo, capsys, how
):
    (feature_repo / '.gitmodules').write_text(GITMODULES)
    add_embedded_repo(feature_repo, 'byceps/services/evilsub')
    stage_or_commit(feature_repo, how)

    label = LABEL_FOR[how]
    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            f'  {label}: unapproved Core path changed: .gitmodules\n'
            f'  {label}: unapproved Core path changed: '
            'byceps/services/evilsub\n'
            'PASS: catalogues\n'
        ),
    )


def test_real_git_rejects_gitlink_hidden_by_ignore_all_at_base(
    submodule_repo, capsys, how
):
    add_embedded_repo(submodule_repo, 'byceps/services/evilsub')
    stage_or_commit(submodule_repo, how)

    assert run_cli(capsys) == (
        1,
        (
            'FAIL: core\n'
            f'  {LABEL_FOR[how]}: unapproved Core path changed: '
            'byceps/services/evilsub\n'
            'PASS: catalogues\n'
        ),
    )


def test_real_git_rejects_catalogue_gitlink_hidden_by_ignore_all(
    submodule_repo,
):
    add_embedded_repo(submodule_repo, 'byceps/translations/de/sub')
    commit = commit_all(submodule_repo, 'catalogues')

    assert boundary.check_catalogues('main') == [
        f'{commit[:9]}: touched generated catalogue byceps/translations/de/sub',
        (
            'index/HEAD: stale generated catalogue: '
            'byceps/translations/de/sub differs from base'
        ),
    ]


def test_real_git_run_never_writes_the_real_index(
    feature_repo, tmp_path, monkeypatch, capsys
):
    repo = feature_repo
    stale = repo / 'byceps/static/x.css'
    stale.write_bytes(stale.read_bytes())
    stat = stale.stat()
    os.utime(stale, ns=(stat.st_atime_ns, stat.st_mtime_ns - 10**10))
    (repo / 'app.py').write_text("print('staged')\n")
    git(repo, 'add', 'app.py')

    monkeypatch.setenv('GIT_OPTIONAL_LOCKS', '0')
    control = tmp_path / 'control'
    shutil.copytree(repo, control)
    control_index = control / '.git' / 'index'
    control_before = control_index.read_bytes()
    git(control, 'diff', '--name-only', 'main')
    assert control_index.read_bytes() != control_before, (
        'precondition: porcelain diff must refresh a stat-dirty index'
    )

    index = repo / '.git' / 'index'
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    seen = []
    copies = []
    real_run = subprocess.run

    def spy(argv, **kwargs):
        copy = kwargs['env'].get('GIT_INDEX_FILE')
        seen.append(copy)
        if copy is not None:
            copies.append(Path(copy).read_bytes())
        return real_run(argv, **kwargs)

    monkeypatch.setattr(boundary.subprocess, 'run', spy)

    code, output = run_cli(capsys)

    assert code == 1
    assert '  index: unapproved Core path changed: app.py\n' in output
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    private = set(seen[1:])
    assert len(private) == 1
    assert None not in private
    assert str(index) not in private
    assert copies
    assert set(copies) == {before[0]}
    assert 'GIT_INDEX_FILE' not in os.environ
