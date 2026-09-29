from pathlib import Path
import shutil
import subprocess

import pytest


_ROOT = Path(__file__).resolve().parents[4]
_SUITE = _ROOT / 'tests/js/lan_tournament_create_wizard_image.test.js'

_NODE_BIN = shutil.which('node')

pytestmark = pytest.mark.skipif(
    _NODE_BIN is None, reason='Node.js is not installed'
)


def test_node_create_wizard_image_suite_passes():
    result = subprocess.run(  # noqa: S603 -- fixed local node binary
        [_NODE_BIN, '--test', str(_SUITE)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
