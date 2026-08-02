"""Small, dependency-free helpers for trusted local file replacement and hashing."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_CHUNK_SIZE = 1024 * 1024
_REPLACE_LOCK_COUNT = 64
_REPLACE_LOCKS = tuple(threading.RLock() for _ in range(_REPLACE_LOCK_COUNT))
_REPLACE_RETRY_DELAYS_SECONDS = (0.01, 0.025, 0.05, 0.1, 0.2)


def _destination_lock(path: Path) -> threading.RLock:
    """Return a bounded process-local lock shared by equivalent destinations."""

    canonical = os.path.normcase(str(path.resolve(strict=False)))
    return _REPLACE_LOCKS[hash(canonical) % _REPLACE_LOCK_COUNT]


def _replace_with_retry(source: Path, destination: Path) -> None:
    """Serialize a destination replace and retry transient Windows sharing errors."""

    with _destination_lock(destination):
        for delay in (*_REPLACE_RETRY_DELAYS_SECONDS, None):
            try:
                os.replace(source, destination)
                return
            except PermissionError:
                if delay is None:
                    raise
                time.sleep(delay)


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
        _replace_with_retry(temporary, destination)
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
        _replace_with_retry(temporary, path)
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
