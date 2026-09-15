"""Canonical JSON + content hashing.

Everything that gets hashed (config_sha, dataset_sha, run_id) goes through `canon()` so the
same content always produces the same digest on Windows and on the box.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any


def canon(obj: Any) -> str:
    """Canonical JSON text: sorted keys, no spaces, UTF-8, LF."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(obj: Any, n: int = 64) -> str:
    """sha256 of the canonical form, truncated to `n` hex chars."""
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()[:n]


def sha_bytes(data: bytes, n: int = 64) -> str:
    return hashlib.sha256(data).hexdigest()[:n]


def sha_file(path: str | pathlib.Path, n: int = 64) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:n]


def read(path: str | pathlib.Path) -> Any:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def write(path: str | pathlib.Path, obj: Any, *, indent: int = 2) -> pathlib.Path:
    """Write pretty JSON with LF endings (the box reads these in tcsh scripts)."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=indent, sort_keys=True, ensure_ascii=False) + "\n"
    p.write_text(text, encoding="utf-8", newline="\n")
    return p
