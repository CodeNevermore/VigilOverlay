"""Read-only game-provider contracts used by Vigil's library aggregator."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PureWindowsPath
from threading import Event
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

_PROVIDER_ID = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_RECENCY_SOURCE = re.compile(r"^[a-z0-9]+(?:[._:-][a-z0-9]+)*$")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")


class GameLaunchTargetKind(StrEnum):
    """Launch mechanisms understood by the host-owned launch service."""

    URI = "uri"
    EXECUTABLE = "executable"


class GameIconKind(StrEnum):
    """Local icon sources that the host may render or extract."""

    LOCAL_IMAGE = "local_image"
    EXECUTABLE = "executable"


@dataclass(frozen=True, slots=True)
class GameProviderDescriptor:
    """Stable identity and display metadata for one provider implementation."""

    provider_id: str
    display_name: str

    def __post_init__(self) -> None:
        if not _PROVIDER_ID.fullmatch(self.provider_id):
            raise ValueError("provider_id must be a lowercase provider identifier")
        _validate_text(self.display_name, "display_name", max_length=160)


@dataclass(frozen=True, slots=True)
class GameIdentity:
    """Provider-scoped identity for one discovered game."""

    provider_id: str
    provider_game_id: str

    def __post_init__(self) -> None:
        if not _PROVIDER_ID.fullmatch(self.provider_id):
            raise ValueError("provider_id must be a lowercase provider identifier")
        _validate_text(self.provider_game_id, "provider_game_id", max_length=256)


@dataclass(frozen=True, slots=True)
class GameRecency:
    """Provider-owned evidence used to order recent games without Vigil activity history."""

    source: str
    confidence: float
    last_played_utc: datetime | None = None
    recent_rank: int | None = None

    def __post_init__(self) -> None:
        if not _RECENCY_SOURCE.fullmatch(self.source):
            raise ValueError("source must be a lowercase recency-source identifier")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be a finite value between 0.0 and 1.0")
        if self.last_played_utc is None and self.recent_rank is None:
            raise ValueError("recency evidence requires last_played_utc or recent_rank")
        if self.last_played_utc is not None:
            normalized = _normalize_utc(self.last_played_utc, "last_played_utc")
            object.__setattr__(self, "last_played_utc", normalized)
        if self.recent_rank is not None and (
            type(self.recent_rank) is not int or self.recent_rank < 1
        ):
            raise ValueError("recent_rank must be a positive integer")


@dataclass(frozen=True, slots=True)
class GameIconReference:
    """Reference to provider-resolved local icon material; None means host placeholder."""

    kind: GameIconKind
    path: str

    def __post_init__(self) -> None:
        _validate_absolute_windows_path(self.path, "icon path")
        if (
            self.kind is GameIconKind.EXECUTABLE
            and PureWindowsPath(self.path).suffix.casefold() != ".exe"
        ):
            raise ValueError("executable icon references must point to an .exe file")


@dataclass(frozen=True, slots=True)
class GameLaunchTarget:
    """Declarative launch request; execution and final validation remain host-owned."""

    kind: GameLaunchTargetKind
    target: str
    arguments: tuple[str, ...] = ()
    working_directory: str | None = None

    def __post_init__(self) -> None:
        if self.kind is GameLaunchTargetKind.URI:
            _validate_uri_target(self.target)
            if self.arguments:
                raise ValueError("URI launch targets cannot include process arguments")
            if self.working_directory is not None:
                raise ValueError(
                    "URI launch targets cannot include a working directory"
                )
            return

        if self.kind is GameLaunchTargetKind.EXECUTABLE:
            _validate_absolute_windows_path(self.target, "executable launch target")
            if PureWindowsPath(self.target).suffix.casefold() != ".exe":
                raise ValueError("executable launch targets must point to an .exe file")
            for index, argument in enumerate(self.arguments):
                _validate_argument(argument, f"arguments[{index}]")
            if self.working_directory is not None:
                _validate_absolute_windows_path(
                    self.working_directory, "working_directory"
                )
            return

        raise ValueError(f"unsupported launch target kind: {self.kind}")


@dataclass(frozen=True, slots=True)
class GameRecord:
    """Normalized read-only game record returned by a provider snapshot."""

    identity: GameIdentity
    title: str
    is_installed: bool
    is_available: bool
    launch_target: GameLaunchTarget | None = None
    install_directory: str | None = None
    icon: GameIconReference | None = None
    recency: GameRecency | None = None

    def __post_init__(self) -> None:
        _validate_text(self.title, "title", max_length=512)
        if type(self.is_installed) is not bool:
            raise ValueError("is_installed must be a boolean")
        if type(self.is_available) is not bool:
            raise ValueError("is_available must be a boolean")
        if self.is_available and self.launch_target is None:
            raise ValueError("available games must provide a launch_target")
        if self.install_directory is not None:
            _validate_absolute_windows_path(self.install_directory, "install_directory")


@dataclass(frozen=True, slots=True)
class GameDiscoveryContext:
    """Cooperative cancellation and deadline context supplied by the aggregator."""

    deadline_monotonic: float | None = None
    cancellation_event: Event | None = None

    def __post_init__(self) -> None:
        if self.deadline_monotonic is not None and (
            not math.isfinite(self.deadline_monotonic) or self.deadline_monotonic < 0.0
        ):
            raise ValueError("deadline_monotonic must be a finite non-negative value")

    def is_cancelled(self) -> bool:
        if self.cancellation_event is not None and self.cancellation_event.is_set():
            return True
        return (
            self.deadline_monotonic is not None
            and time.monotonic() >= self.deadline_monotonic
        )

    def remaining_seconds(self) -> float | None:
        if self.deadline_monotonic is None:
            return None
        return max(0.0, self.deadline_monotonic - time.monotonic())


@dataclass(frozen=True, slots=True)
class GameProviderSnapshot:
    """One provider-owned discovery snapshot suitable for bounded caching by the host."""

    provider: GameProviderDescriptor
    games: tuple[GameRecord, ...]
    captured_at_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    complete: bool = True
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "captured_at_utc",
            _normalize_utc(self.captured_at_utc, "captured_at_utc"),
        )
        if type(self.complete) is not bool:
            raise ValueError("complete must be a boolean")

        seen: set[GameIdentity] = set()
        for game in self.games:
            if game.identity.provider_id != self.provider.provider_id:
                raise ValueError("snapshot games must belong to the snapshot provider")
            if game.identity in seen:
                raise ValueError(
                    f"duplicate provider game ID: {game.identity.provider_game_id}"
                )
            seen.add(game.identity)

        for index, warning in enumerate(self.warnings):
            _validate_text(warning, f"warnings[{index}]", max_length=2_048)


@runtime_checkable
class GameProvider(Protocol):
    """Read-only provider boundary consumed by the game-library aggregator."""

    @property
    def descriptor(self) -> GameProviderDescriptor: ...

    def discover_games(self, context: GameDiscoveryContext) -> GameProviderSnapshot: ...


def _validate_text(value: str, field_name: str, *, max_length: int) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(
            f"{field_name} must be non-empty text without surrounding whitespace"
        )
    if len(value) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{field_name} contains an unsupported control character")


def _normalize_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_absolute_windows_path(value: str, field_name: str) -> None:
    _validate_text(value, field_name, max_length=32_767)
    if not PureWindowsPath(value).is_absolute():
        raise ValueError(f"{field_name} must be an absolute Windows path")


def _validate_uri_target(value: str) -> None:
    _validate_text(value, "URI launch target", max_length=4_096)
    parsed = urlsplit(value)
    if not parsed.scheme or not _URI_SCHEME.fullmatch(parsed.scheme):
        raise ValueError("URI launch target must include a valid scheme")
    if parsed.scheme.casefold() == "file":
        raise ValueError("file: launch targets must use the executable launch kind")


def _validate_argument(value: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if len(value) > 8_192:
        raise ValueError(f"{field_name} exceeds 8192 characters")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{field_name} contains an unsupported control character")


__all__ = [
    "GameDiscoveryContext",
    "GameIconKind",
    "GameIconReference",
    "GameIdentity",
    "GameLaunchTarget",
    "GameLaunchTargetKind",
    "GameProvider",
    "GameProviderDescriptor",
    "GameProviderSnapshot",
    "GameRecency",
    "GameRecord",
]
