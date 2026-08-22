"""AkShare proxy switch tests."""

import os
from pathlib import Path
import subprocess
import sys


def test_disabled_proxy_does_not_import_patch_package():
    env = os.environ.copy()
    env["AKSHARE_PROXY_ENABLED"] = "0"
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import qtf_mcp; "
                "assert 'akshare_proxy_patch' not in sys.modules"
            ),
        ],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
