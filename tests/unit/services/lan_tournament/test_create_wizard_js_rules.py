import json
from pathlib import Path
import shutil
import subprocess

import pytest

from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    _get_valid_combinations,
    CREATE_WIZARD_STEP_FIELDS,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import (
    GameFormat,
    is_valid_combination,
)


_ROOT = Path(__file__).resolve().parents[4]
_RULES = _ROOT / 'byceps/static/behavior/lan_tournament_create_wizard_rules.js'
_SUITE = _ROOT / 'tests/js/lan_tournament_create_wizard_rules.test.js'

_NODE_BIN = shutil.which('node')

pytestmark = pytest.mark.skipif(
    _NODE_BIN is None, reason='Node.js is not installed'
)


def test_node_rules_suite_passes():
    result = subprocess.run(  # noqa: S603 -- fixed local node binary
        [_NODE_BIN, '--test', str(_SUITE)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_js_step_fields_match_python():
    result = subprocess.run(  # noqa: S603 -- fixed local node binary
        [
            _NODE_BIN,
            '-e',
            f'console.log(JSON.stringify(require({str(_RULES)!r}).STEP_FIELDS))',
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    js_fields = json.loads(result.stdout)

    assert js_fields == [list(fields) for fields in CREATE_WIZARD_STEP_FIELDS]


def test_js_mode_area_matches_the_server_combinations():
    script = (
        f'const r = require({str(_RULES)!r});'
        'const combos = JSON.parse(process.argv[1]);'
        'const out = {};'
        f'for (const f of {[f.name for f in GameFormat]!r}) '
        '{ out[f] = r.modeArea(f, combos); }'
        'console.log(JSON.stringify(out));'
    )
    result = subprocess.run(  # noqa: S603 -- fixed local node binary
        [_NODE_BIN, '-e', script, json.dumps(_get_valid_combinations())],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    areas = json.loads(result.stdout)

    for game_format in GameFormat:
        area = areas[game_format.name]
        assert area['view'] == (
            'fixed' if game_format is GameFormat.HIGHSCORE else 'cards'
        )
        for choice in area['modes']:
            mode = EliminationMode[choice['mode']]
            assert choice['available'] is is_valid_combination(
                game_format, mode
            ), (game_format, mode)
            assert choice['offered'] is (mode is not EliminationMode.NONE)
