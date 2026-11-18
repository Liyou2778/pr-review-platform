"""Project-local temp dirs.

``tempfile.mkdtemp`` honours ``TMP``/``TEMP``, which on some Windows shells is
the current directory — that litters the repo with ``ocr-ws-*`` folders and can
accidentally get committed.  Everything temporary goes under ``.tmp/`` instead,
which is git-ignored and easy to clean.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp"


def workdir(prefix: str) -> Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(TMP_ROOT)))


def cleanup(path: str | Path) -> None:
    if not path:
        return
    shutil.rmtree(str(path), ignore_errors=True)
