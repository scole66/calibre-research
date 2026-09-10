from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

FIELDS = "id,title,authors,isbn,publisher,pubdate,series,series_index,languages"


class CalibreError(RuntimeError):
    pass


def scan_library(library: Path) -> list[dict]:
    exe = shutil.which("calibredb")
    if not exe:
        raise CalibreError("calibredb was not found in PATH")

    cmd = [
        exe,
        "list",
        "--with-library",
        str(library),
        "--fields",
        FIELDS,
        "--for-machine",
    ]

    proc = subprocess.run(cmd, text=True, capture_output=True)

    if proc.returncode != 0:
        raise CalibreError(proc.stderr.strip() or "calibredb list failed")

    try:
        books = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise CalibreError(f"invalid JSON from calibredb: {exc}") from exc

    if not isinstance(books, list):
        raise CalibreError(
            f"unexpected calibredb result: expected list, got {type(books).__name__}"
        )

    return books
