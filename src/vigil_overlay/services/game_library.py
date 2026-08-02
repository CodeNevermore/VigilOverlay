"""Provider registry and read-only game-library aggregation."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import PureWindowsPath
from threading import Event
from urllib.parse import urlsplit

from vigil_overlay.contracts.games import (
    GameDiscoveryContext,
    GameIdentity,
    GameLaunchTargetKind,
    GameProvider,
    GameProviderSnapshot,
    GameRecency,
    GameRecord,
)

_LOGGER = logging.getLogger("vigil_overlay")


@dataclass(frozen=True, slots=True)
class GameProviderRegistration:
    """Host-owned provider policy, including deterministic cross-provider precedence."""

    provider: GameProvider
    enabled: bool = True
    allowed_uri_schemes: tuple[str, ...] = ()
    aggregation_priority: int = 1_000

    def __post_init__(self) -> None:
        normalized = tuple(scheme.casefold() for scheme in self.allowed_uri_schemes)
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed_uri_schemes must not contain duplicates")
        for scheme in normalized:
            if not scheme or any(character.isspace() for character in scheme):
                raise ValueError(
                    "allowed URI schemes must be non-empty and contain no whitespace"
                )
        if type(self.aggregation_priority) is not int or self.aggregation_priority < 0:
            raise ValueError("aggregation_priority must be a non-negative integer")
        object.__setattr__(self, "allowed_uri_schemes", normalized)


class GameProviderRegistry:
    """Deterministic registry for built-in and host-trusted game providers."""

    def __init__(self) -> None:
        self._registrations: dict[str, GameProviderRegistration] = {}
        self._order: list[str] = []

    def register(self, registration: GameProviderRegistration) -> None:
        provider_id = registration.provider.descriptor.provider_id
        if provider_id in self._registrations:
            raise ValueError(f"duplicate game provider ID: {provider_id}")
        self._registrations[provider_id] = registration
        self._order.append(provider_id)

    def registration(self, provider_id: str) -> GameProviderRegistration:
        try:
            return self._registrations[provider_id]
        except KeyError as exc:
            raise ValueError(f"unknown game provider ID: {provider_id}") from exc

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(self._order)

    def enabled_registrations(self) -> tuple[GameProviderRegistration, ...]:
        return tuple(
            self._registrations[provider_id]
            for provider_id in self._order
            if self._registrations[provider_id].enabled
        )

    def set_enabled(self, provider_id: str, enabled: bool) -> None:
        registration = self.registration(provider_id)
        self._registrations[provider_id] = GameProviderRegistration(
            provider=registration.provider,
            enabled=enabled,
            allowed_uri_schemes=registration.allowed_uri_schemes,
            aggregation_priority=registration.aggregation_priority,
        )

    def validates_launch_target(self, game: GameRecord) -> bool:
        target = game.launch_target
        if target is None:
            return False
        if target.kind is GameLaunchTargetKind.EXECUTABLE:
            return True
        registration = self.registration(game.identity.provider_id)
        scheme = urlsplit(target.target).scheme.casefold()
        return scheme in registration.allowed_uri_schemes


@dataclass(frozen=True, slots=True)
class ProviderAggregationResult:
    """Outcome for one provider without allowing one failure to poison the library."""

    provider_id: str
    snapshot: GameProviderSnapshot | None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.snapshot is not None and self.error is None


@dataclass(frozen=True, slots=True)
class AggregatedGameLibrary:
    """Provider results plus one canonical game record for each known cross-provider title."""

    games: tuple[GameRecord, ...]
    provider_results: tuple[ProviderAggregationResult, ...]

    def game(self, identity: GameIdentity) -> GameRecord | None:
        for game in self.games:
            if game.identity == identity:
                return game
        return None


class GameLibraryAggregator:
    """Aggregate providers by priority with failure isolation and cross-provider deduplication."""

    def __init__(
        self,
        registry: GameProviderRegistry,
        *,
        provider_timeout_seconds: float = 8.0,
    ) -> None:
        if provider_timeout_seconds <= 0.0:
            raise ValueError("provider_timeout_seconds must be positive")
        self._registry = registry
        self._provider_timeout_seconds = provider_timeout_seconds
        self._cached_results: dict[str, ProviderAggregationResult] = {}

    def aggregate(
        self,
        *,
        provider_id: str | None = None,
        cancellation_event: Event | None = None,
    ) -> AggregatedGameLibrary:
        registrations = tuple(
            registration
            for _, registration in sorted(
                enumerate(self._registry.enabled_registrations()),
                key=lambda item: (item[1].aggregation_priority, item[0]),
            )
        )
        enabled_ids = {item.provider.descriptor.provider_id for item in registrations}
        if provider_id is not None and provider_id not in enabled_ids:
            raise ValueError(f"unknown or disabled game provider ID: {provider_id}")

        for registration in registrations:
            current_id = registration.provider.descriptor.provider_id
            if provider_id is not None and current_id != provider_id:
                continue
            if cancellation_event is not None and cancellation_event.is_set():
                break
            result = self._discover_provider(
                registration,
                cancellation_event=cancellation_event,
            )
            previous = self._cached_results.get(current_id)
            if (
                result.snapshot is None
                and previous is not None
                and previous.snapshot is not None
            ):
                result = ProviderAggregationResult(
                    provider_id=current_id,
                    snapshot=previous.snapshot,
                    error=result.error,
                )
            self._cached_results[current_id] = result

        ordered_results = tuple(
            self._cached_results[current_id]
            for current_id in (
                registration.provider.descriptor.provider_id
                for registration in registrations
            )
            if current_id in self._cached_results
        )
        priorities = {
            registration.provider.descriptor.provider_id: registration.aggregation_priority
            for registration in registrations
        }
        return _aggregate_provider_results(ordered_results, priorities)

    def _discover_provider(
        self,
        registration: GameProviderRegistration,
        *,
        cancellation_event: Event | None,
    ) -> ProviderAggregationResult:
        provider = registration.provider
        provider_id = provider.descriptor.provider_id
        context = GameDiscoveryContext(
            deadline_monotonic=time.monotonic() + self._provider_timeout_seconds,
            cancellation_event=cancellation_event,
        )
        try:
            snapshot = provider.discover_games(context)
            if snapshot.provider.provider_id != provider_id:
                raise ValueError(
                    "provider returned a snapshot owned by a different provider"
                )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning("Game provider %s failed: %s", provider_id, message)
            return ProviderAggregationResult(
                provider_id=provider_id, snapshot=None, error=message
            )
        return ProviderAggregationResult(provider_id=provider_id, snapshot=snapshot)


def _aggregate_provider_results(
    results: tuple[ProviderAggregationResult, ...],
    priorities: dict[str, int],
) -> AggregatedGameLibrary:
    games: list[GameRecord] = []
    game_priorities: list[int] = []
    install_index: dict[str, list[int]] = {}
    title_index: dict[str, list[int]] = {}

    for result in results:
        snapshot = result.snapshot
        if snapshot is None:
            continue
        priority = priorities[result.provider_id]
        for game in snapshot.games:
            duplicate_index = _find_cross_provider_duplicate(
                games,
                game,
                install_index=install_index,
                title_index=title_index,
            )
            if duplicate_index is None:
                duplicate_index = len(games)
                games.append(game)
                game_priorities.append(priority)
                _index_game_aliases(
                    game,
                    duplicate_index,
                    install_index=install_index,
                    title_index=title_index,
                )
                continue
            current = games[duplicate_index]
            current_priority = game_priorities[duplicate_index]
            prefer_candidate = _prefer_game_record(
                current,
                game,
                current_priority=current_priority,
                candidate_priority=priority,
            )
            preferred = game if prefer_candidate else current
            freshest_recency = _freshest_timestamp_recency(
                current.recency,
                game.recency,
                fallback=preferred.recency,
            )
            games[duplicate_index] = (
                preferred
                if preferred.recency == freshest_recency
                else replace(preferred, recency=freshest_recency)
            )
            if prefer_candidate:
                game_priorities[duplicate_index] = priority
            _index_game_aliases(
                game,
                duplicate_index,
                install_index=install_index,
                title_index=title_index,
            )

    return AggregatedGameLibrary(games=tuple(games), provider_results=results)


_TITLE_SPACE = re.compile(r"\s+")


def _find_cross_provider_duplicate(
    existing_games: list[GameRecord],
    candidate: GameRecord,
    *,
    install_index: dict[str, list[int]],
    title_index: dict[str, list[int]],
) -> int | None:
    """Return an indexed equivalent cross-provider record without scanning the whole library."""

    candidate_indexes: set[int] = set()
    install_key = _normalized_install_directory(candidate.install_directory)
    if install_key is not None:
        candidate_indexes.update(install_index.get(install_key, ()))
    candidate_indexes.update(
        title_index.get(_normalized_game_title(candidate.title), ())
    )

    for index in sorted(candidate_indexes):
        existing = existing_games[index]
        if existing.identity == candidate.identity:
            return index
        if existing.identity.provider_id == candidate.identity.provider_id:
            continue
        if _games_are_equivalent(existing, candidate):
            return index
    return None


def _index_game_aliases(
    game: GameRecord,
    index: int,
    *,
    install_index: dict[str, list[int]],
    title_index: dict[str, list[int]],
) -> None:
    install_key = _normalized_install_directory(game.install_directory)
    if install_key is not None:
        _append_unique_index(install_index, install_key, index)
    _append_unique_index(title_index, _normalized_game_title(game.title), index)


def _append_unique_index(index_map: dict[str, list[int]], key: str, index: int) -> None:
    indexes = index_map.setdefault(key, [])
    if index not in indexes:
        indexes.append(index)


def _games_are_equivalent(left: GameRecord, right: GameRecord) -> bool:
    left_install = _normalized_install_directory(left.install_directory)
    right_install = _normalized_install_directory(right.install_directory)
    if left_install is not None and right_install is not None:
        return left_install == right_install
    return _normalized_game_title(left.title) == _normalized_game_title(right.title)


def _normalized_install_directory(path: str | None) -> str | None:
    if path is None:
        return None
    return str(PureWindowsPath(path)).replace("/", "\\").rstrip("\\").casefold()


def _normalized_game_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return _TITLE_SPACE.sub(" ", normalized).strip()


def _prefer_game_record(
    current: GameRecord,
    candidate: GameRecord,
    *,
    current_priority: int,
    candidate_priority: int,
) -> bool:
    """Prefer launchable records first, then the provider's configured aggregation priority."""

    if candidate.is_available != current.is_available:
        return candidate.is_available
    if candidate.is_installed != current.is_installed:
        return candidate.is_installed
    return candidate_priority < current_priority


def _freshest_timestamp_recency(
    current: GameRecency | None,
    candidate: GameRecency | None,
    *,
    fallback: GameRecency | None,
) -> GameRecency | None:
    """Keep the newest comparable provider timestamp on the preferred launch record."""

    timestamped = tuple(
        recency
        for recency in (current, candidate)
        if recency is not None and recency.last_played_utc is not None
    )
    if not timestamped:
        return fallback
    return max(
        timestamped,
        key=lambda recency: recency.last_played_utc or datetime.min,
    )


def select_recent_games(
    library: AggregatedGameLibrary,
    *,
    limit: int = 6,
) -> tuple[GameRecord, ...]:
    """Select recent games without inventing chronology across incomparable rank sources."""

    if limit < 0:
        raise ValueError("limit cannot be negative")
    eligible = [
        game
        for game in library.games
        if game.is_available
        and game.launch_target is not None
        and game.recency is not None
    ]
    timestamped = [
        game
        for game in eligible
        if game.recency is not None and game.recency.last_played_utc is not None
    ]
    timestamped.sort(key=_timestamp_sort_key, reverse=True)
    selected = timestamped[:limit]
    if len(selected) >= limit:
        return tuple(selected)

    timestamped_ids = {game.identity for game in timestamped}
    ranked_by_provider: dict[str, list[GameRecord]] = {}
    for game in eligible:
        if (
            game.identity in timestamped_ids
            or game.recency is None
            or game.recency.recent_rank is None
        ):
            continue
        ranked_by_provider.setdefault(game.identity.provider_id, []).append(game)

    # A provider-local rank has no safe cross-provider meaning. Only consume ranked-only
    # evidence when exactly one provider contributes it; otherwise show fewer than six.
    if len(ranked_by_provider) != 1:
        return tuple(selected)
    ranked = next(iter(ranked_by_provider.values()))
    ranked.sort(key=_rank_sort_key)
    selected.extend(ranked[: max(0, limit - len(selected))])
    return tuple(selected)


def _timestamp_sort_key(game: GameRecord) -> datetime:
    recency = game.recency
    if recency is None or recency.last_played_utc is None:
        raise ValueError("timestamp sort requires last_played_utc")
    return recency.last_played_utc


def _rank_sort_key(game: GameRecord) -> int:
    recency = game.recency
    if recency is None or recency.recent_rank is None:
        raise ValueError("rank sort requires recent_rank")
    return recency.recent_rank


__all__ = [
    "AggregatedGameLibrary",
    "GameLibraryAggregator",
    "GameProviderRegistration",
    "GameProviderRegistry",
    "ProviderAggregationResult",
    "select_recent_games",
]
