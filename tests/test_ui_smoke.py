"""The kitchen screen must boot, open its SSE stream and render a snapshot.

app.js once called two functions that a refactor had deleted: boot() threw before connect(), so
the page stayed on "CHỜ DỮ LIỆU" forever while the whole pipeline was healthy. tests/ui_smoke.js
runs the real script against a minimal fake DOM; it needs Node.js (present on CI runners).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from aiot_pipeline.demo_ui import STATIC_DIR

HARNESS = Path(__file__).with_name("ui_smoke.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_app_js_boots_and_renders_a_live_snapshot() -> None:
    result = subprocess.run(
        ["node", str(HARNESS), str(STATIC_DIR / "app.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
