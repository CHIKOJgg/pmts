from __future__ import annotations

import os
import tempfile
from pathlib import Path


_TMP_ROOT = Path(__file__).resolve().parents[1] / ".pytest-tmp"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

# Keep pytest's temp directories inside the workspace so Windows temp ACLs
# do not break fixtures like tmp_path/tmp_path_factory on this machine.
for key in ("TMPDIR", "TEMP", "TMP"):
    os.environ[key] = str(_TMP_ROOT)
tempfile.tempdir = str(_TMP_ROOT)

