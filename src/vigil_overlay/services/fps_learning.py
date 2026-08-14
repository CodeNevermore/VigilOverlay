"""Durable executable evidence learned from frame-verified FPS streams."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Final

from vigil_overlay.contracts.games import GameIdentity
from vigil_overlay.core.file_io import atomic_write_json

_LOGGER = logging.getLogger("vigil_overlay")
_SCHEMA_VERSION: Final[int] = 2
_SUPPORTED_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset({1, _SCHEMA_VERSION})
_MAX_CACHE_BYTES: Final[int] = 4 * 1024 * 1024
_MAX_PATH_LENGTH: Final[int] = 4_096


@dataclass(frozen=True, slots=True)
class LearnedFpsExecutable:
    """One executable that previously produced usable FPS frames."""

    executable_path: str
    identity: GameIdentity | None = None

    @property
    def provider_bound(self) -> bool:
        return self.identity is not None


class LearnedFpsGameCache:
    """Load and atomically persist frame-verified executable evidence.

    Normal operation never expires, ages, or count-limits valid mappings. The byte
    limit is only a defensive boundary against unreasonable or malformed local data.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        entries, needs_rewrite = _load_entries(path)
        self._entries = entries
        self._needs_rewrite = needs_rewrite
        if needs_rewrite:
            try:
                self._persist(entries)
            except OSError:
                _LOGGER.exception("Could not sanitize learned FPS game cache: %s", path)
            else:
                self._needs_rewrite = False

    @property
    def entries(self) -> tuple[LearnedFpsExecutable, ...]:
        with self._lock:
            return self._entries

    def record(
        self,
        identity: GameIdentity | None,
        executable_path: str,
    ) -> bool:
        """Persist new verified evidence without persisting its PID or activity history.

        A provider match upgrades a path-only local record. Returning ``True`` means the
        durable cache changed; an already known mapping returns ``False``.
        """

        normalized_path = _normalized_executable_path(executable_path)
        if normalized_path is None:
            return False
        normalized_key = _path_key(normalized_path)
        new_entry = LearnedFpsExecutable(
            executable_path=normalized_path,
            identity=identity,
        )
        with self._lock:
            matching = tuple(
                entry
                for entry in self._entries
                if _path_key(entry.executable_path) == normalized_key
            )
            if any(entry.identity == identity for entry in matching) or (
                identity is None and matching
            ):
                if self._needs_rewrite:
                    self._persist(self._entries)
                    self._needs_rewrite = False
                return False

            entries = list(self._entries)
            if identity is not None:
                entries = [
                    entry
                    for entry in entries
                    if not (
                        entry.identity is None
                        and _path_key(entry.executable_path) == normalized_key
                    )
                ]
            entries.append(new_entry)
            canonical_entries = tuple(_canonical_entries(entries))
            if not _payload_fits(canonical_entries):
                _LOGGER.warning(
                    "Learned FPS game cache reached its defensive size limit; "
                    "the new mapping was not persisted: %s",
                    normalized_path,
                )
                return False
            self._persist(canonical_entries)
            self._entries = canonical_entries
            self._needs_rewrite = False
        return True

    def _persist(self, entries: tuple[LearnedFpsExecutable, ...]) -> None:
        payload = _cache_payload(entries)
        if not _serialized_payload_fits(payload):
            raise OSError("learned FPS game cache exceeds defensive size limit")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path, payload, compact=False)


def _load_entries(path: Path) -> tuple[tuple[LearnedFpsExecutable, ...], bool]:
    if not path.is_file():
        return (), False
    try:
        if path.stat().st_size > _MAX_CACHE_BYTES:
            _LOGGER.warning("Ignored oversized learned FPS game cache: %s", path)
            return (), False
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _LOGGER.warning("Ignored unreadable learned FPS game cache: %s", path)
        return (), False
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") not in _SUPPORTED_SCHEMA_VERSIONS
    ):
        _LOGGER.warning("Ignored unsupported learned FPS game cache: %s", path)
        return (), False
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        _LOGGER.warning("Ignored malformed learned FPS game cache: %s", path)
        return (), False
    entries = tuple(
        _canonical_entries(
            parsed
            for raw_entry in raw_entries
            for parsed in (_parse_entry(raw_entry),)
            if parsed is not None
        )
    )
    canonical_payload = _cache_payload(entries)
    return entries, payload != canonical_payload


def _parse_entry(raw_entry: object) -> LearnedFpsExecutable | None:
    if not isinstance(raw_entry, Mapping):
        return None
    executable_path = raw_entry.get("executable_path")
    if not isinstance(executable_path, str):
        return None
    normalized_path = _normalized_executable_path(executable_path)
    if normalized_path is None:
        return None

    source = raw_entry.get("source", "provider")
    if source == "local":
        return LearnedFpsExecutable(executable_path=normalized_path)
    if source != "provider":
        return None
    provider_id = raw_entry.get("provider_id")
    provider_game_id = raw_entry.get("provider_game_id")
    if not isinstance(provider_id, str) or not isinstance(provider_game_id, str):
        return None
    try:
        identity = GameIdentity(provider_id, provider_game_id)
    except ValueError:
        return None
    return LearnedFpsExecutable(
        executable_path=normalized_path,
        identity=identity,
    )


def _normalized_executable_path(path: str) -> str | None:
    cleaned = path.strip()
    if not cleaned or len(cleaned) > _MAX_PATH_LENGTH:
        return None
    parsed = PureWindowsPath(cleaned)
    if not parsed.is_absolute() or parsed.suffix.casefold() != ".exe":
        return None
    return str(parsed)


def _path_key(executable_path: str) -> str:
    return str(PureWindowsPath(executable_path)).casefold()


def _entry_key(entry: LearnedFpsExecutable) -> tuple[str, str, str]:
    identity = entry.identity
    return (
        _path_key(entry.executable_path),
        identity.provider_id if identity is not None else "",
        identity.provider_game_id if identity is not None else "",
    )


def _canonical_entries(
    entries: Iterable[LearnedFpsExecutable],
) -> list[LearnedFpsExecutable]:
    typed_entries = list(entries)
    canonical: list[LearnedFpsExecutable] = []
    seen: set[tuple[str, str, str]] = set()
    provider_paths = {
        _path_key(entry.executable_path) for entry in typed_entries if entry.identity is not None
    }
    for entry in sorted(typed_entries, key=_entry_key):
        if entry.identity is None and _path_key(entry.executable_path) in provider_paths:
            continue
        key = _entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        canonical.append(entry)
    return canonical


def _entry_payload(entry: LearnedFpsExecutable) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": "provider" if entry.identity is not None else "local",
        "executable_path": entry.executable_path,
    }
    if entry.identity is not None:
        payload["provider_id"] = entry.identity.provider_id
        payload["provider_game_id"] = entry.identity.provider_game_id
    return payload


def _cache_payload(entries: tuple[LearnedFpsExecutable, ...]) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "entries": [_entry_payload(entry) for entry in entries],
    }


def _serialized_payload_fits(payload: Mapping[str, object]) -> bool:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded) <= _MAX_CACHE_BYTES


def _payload_fits(entries: tuple[LearnedFpsExecutable, ...]) -> bool:
    return _serialized_payload_fits(_cache_payload(entries))


__all__ = ["LearnedFpsExecutable", "LearnedFpsGameCache"]
