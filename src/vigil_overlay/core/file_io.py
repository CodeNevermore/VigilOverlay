"""Small, dependency-free helpers for trusted local file replacement and hashing."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest for *path* without loading it all into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_copy_file(
    source: Path,
    destination: Path,
    *,
    temporary_suffix: str = ".vigil-tmp",
) -> None:
    """Copy a file and atomically replace *destination* in the same directory."""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=temporary_suffix,
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    temporary_suffix: str = ".vigil-tmp",
    fsync: bool = False,
) -> None:
    """Write UTF-8 text and atomically replace *path* in the same directory."""

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=temporary_suffix,
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        assert temporary is not None
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    compact: bool = True,
    temporary_suffix: str = ".vigil-tmp",
) -> None:
    """Serialize a JSON object and atomically replace *path*."""

    if compact:
        text = json.dumps(payload, separators=(",", ":"))
    else:
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text, temporary_suffix=temporary_suffix)


__all__ = [
    "atomic_copy_file",
    "atomic_write_json",
    "atomic_write_text",
    "sha256_file",
]
