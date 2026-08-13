"""Provider-aware policy for selecting a small set of real FPS targets."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Final

from vigil_overlay.contracts.games import GameLaunchTargetKind, GameRecord
from vigil_overlay.services.fps import FpsTarget

_LOGGER = logging.getLogger("vigil_overlay")
_DEFAULT_MAX_CANDIDATES: Final[int] = 3
_DEFAULT_MINIMUM_GPU_PERCENT: Final[float] = 1.0
_DEFAULT_REQUIRED_GPU_OBSERVATIONS: Final[int] = 2


@dataclass(frozen=True, slots=True)
class _IndexedGame:
    provider_id: str
    title: str
    executable_path: tuple[str, ...] | None
    executable_name: str | None
    install_directory: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _ProviderMatch:
    rank: int
    game: _IndexedGame
    reason: str


class FpsCandidateSelector:
    """Prefer provider-confirmed games and require sustained GPU use for fallbacks."""

    def __init__(
        self,
        *,
        max_candidates: int = _DEFAULT_MAX_CANDIDATES,
        minimum_gpu_percent: float = _DEFAULT_MINIMUM_GPU_PERCENT,
        required_gpu_observations: int = _DEFAULT_REQUIRED_GPU_OBSERVATIONS,
    ) -> None:
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        if minimum_gpu_percent < 0.0:
            raise ValueError("minimum_gpu_percent must not be negative")
        if required_gpu_observations <= 0:
            raise ValueError("required_gpu_observations must be positive")
        self._max_candidates = max_candidates
        self._minimum_gpu_percent = minimum_gpu_percent
        self._required_gpu_observations = required_gpu_observations
        self._lock = threading.Lock()
        self._games: tuple[_IndexedGame, ...] = ()
        self._gpu_observations: dict[tuple[int, int | str], int] = {}
        self._last_logged_selection: tuple[tuple[int, int | str], ...] | None = None

    def update_known_games(self, games: tuple[GameRecord, ...]) -> None:
        indexed = tuple(
            item
            for game in games
            if game.is_installed
            for item in (_index_game(game),)
            if item is not None
        )
        with self._lock:
            self._games = indexed
            self._last_logged_selection = None

    def select(self, candidates: tuple[FpsTarget, ...]) -> tuple[FpsTarget, ...]:
        with self._lock:
            games = self._games
            previous_observations = self._gpu_observations

            unique: list[FpsTarget] = []
            seen_pids: set[int] = set()
            for candidate in candidates:
                if candidate.process_id in seen_pids:
                    continue
                seen_pids.add(candidate.process_id)
                unique.append(candidate)

            current_observations: dict[tuple[int, int | str], int] = {}
            for candidate in unique:
                usage = candidate.gpu_usage_percent
                if usage is None or usage < self._minimum_gpu_percent:
                    continue
                current_observations[candidate.identity_key] = (
                    previous_observations.get(candidate.identity_key, 0) + 1
                )
            self._gpu_observations = current_observations

            scored: list[tuple[int, float, int, FpsTarget, str]] = []
            for index, candidate in enumerate(unique):
                provider_match = _match_provider_game(candidate, games)
                if provider_match is not None:
                    source = (
                        f"provider={provider_match.game.provider_id} "
                        f"game={provider_match.game.title!r} "
                        f"match={provider_match.reason}"
                    )
                    rank = provider_match.rank
                elif (
                    current_observations.get(candidate.identity_key, 0)
                    >= self._required_gpu_observations
                ):
                    source = "sustained-gpu-fallback"
                    rank = 3
                else:
                    continue
                usage = candidate.gpu_usage_percent or 0.0
                scored.append((rank, -usage, index, candidate, source))

            scored.sort(key=lambda item: item[:3])
            selected_rows = scored[: self._max_candidates]
            selected = tuple(item[3] for item in selected_rows)
            signature = tuple(candidate.identity_key for candidate in selected)
            if signature != self._last_logged_selection:
                if selected_rows:
                    summary = ", ".join(
                        f"{candidate.executable_name}:{candidate.gpu_usage_percent or 0.0:.1f}% "
                        f"({source})"
                        for _rank, _usage, _index, candidate, source in selected_rows
                    )
                    _LOGGER.info("FPS target policy selected: %s", summary)
                else:
                    _LOGGER.debug(
                        "FPS target policy found no provider match or sustained GPU candidate"
                    )
                self._last_logged_selection = signature
            return selected


def _index_game(game: GameRecord) -> _IndexedGame | None:
    target = game.launch_target
    executable_path: tuple[str, ...] | None = None
    executable_name: str | None = None
    if target is not None and target.kind is GameLaunchTargetKind.EXECUTABLE:
        executable_path = _windows_path_key(target.target)
        executable_name = PureWindowsPath(target.target).name.casefold()
    install_directory = (
        _windows_path_key(game.install_directory) if game.install_directory is not None else None
    )
    if executable_path is None and install_directory is None:
        return None
    return _IndexedGame(
        provider_id=game.identity.provider_id,
        title=game.title,
        executable_path=executable_path,
        executable_name=executable_name,
        install_directory=install_directory,
    )


def _match_provider_game(
    candidate: FpsTarget,
    games: tuple[_IndexedGame, ...],
) -> _ProviderMatch | None:
    candidate_path = (
        _windows_path_key(candidate.executable_path)
        if candidate.executable_path is not None
        else None
    )
    candidate_name = PureWindowsPath(candidate.executable_name).name.casefold()
    best: _ProviderMatch | None = None
    for game in games:
        match: _ProviderMatch | None = None
        if candidate_path is not None and candidate_path == game.executable_path:
            match = _ProviderMatch(0, game, "exact-executable")
        elif (
            candidate_path is not None
            and game.install_directory is not None
            and _windows_path_is_within(candidate_path, game.install_directory)
        ):
            match = _ProviderMatch(1, game, "install-directory")
        elif game.executable_name is not None and candidate_name == game.executable_name:
            match = _ProviderMatch(2, game, "executable-name")
        if match is not None and (best is None or match.rank < best.rank):
            best = match
            if best.rank == 0:
                break
    return best


def _windows_path_key(path: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in PureWindowsPath(path).parts)


def _windows_path_is_within(
    path: tuple[str, ...],
    root: tuple[str, ...],
) -> bool:
    return len(path) > len(root) and path[: len(root)] == root


__all__ = ["FpsCandidateSelector"]
