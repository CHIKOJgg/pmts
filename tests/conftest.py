from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

pytest_plugins = ("tests.venue_fixtures",)

# Keep pytest's temp directories inside the workspace so Windows temp ACLs
# do not break fixtures like tmp_path/tmp_path_factory on this machine.
# A fresh, unique session directory is used each run so a stale/locked
# directory left behind by a crashed run can never block fixture setup.
_SESSION_ID = f"{os.getpid()}-{int(time.time() * 1000)}"
_TMP_ROOT = Path(__file__).resolve().parents[1] / ".pytest-tmp" / f"run-{_SESSION_ID}"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

for key in ("TMPDIR", "TEMP", "TMP"):
    os.environ[key] = str(_TMP_ROOT)
tempfile.tempdir = str(_TMP_ROOT)
