"""Provider-aware and frame-learned policy for selecting real FPS targets."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Final

from vigil_overlay.contracts.games import GameIdentity, GameLaunchTargetKind, GameRecord
from vigil_overlay.services.fps import FpsTarget
from vigil_overlay.services.fps_learning import LearnedFpsExecutable, LearnedFpsGameCache

_LOGGER = logging.getLogger("vigil_overlay")
_DEFAULT_MAX_CANDIDATES: Final[int] = 8


@dataclass(frozen=True, slots=True)
class _IndexedGame:
    identity: GameIdentity
    title: str
    executable_path: tuple[str, ...] | None
    executable_name: str | None
    install_directory: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _CandidateEvidence:
    rank: int
    identity: GameIdentity | None
    title: str
    reason: str
    learned: bool


@dataclass(frozen=True, slots=True)
class FpsCandidateMatch:
    """Durable learned or current provider evidence for one process candidate."""

    identity: GameIdentity | None
    title: str
    reason: str
    learned: bool

    @property
    def provider_bound(self) -> bool:
        return self.identity is not None


class FpsCandidateSelector:
    """Rank learned executables first, then visible installed-provider matches."""

    def __init__(
        self,
        *,
        max_candidates: int = _DEFAULT_MAX_CANDIDATES,
        learned_cache: LearnedFpsGameCache | None = None,
    ) -> None:
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        self._max_candidates = max_candidates
        self._lock = threading.Lock()
        self._games: tuple[_IndexedGame, ...] = ()
        self._learned_cache = learned_cache
        self._learned_entries = learned_cache.entries if learned_cache is not None else ()
        self._last_logged_selection: tuple[tuple[int, int | str], ...] | None = None

    @property
    def has_targeting_evidence(self) -> bool:
        with self._lock:
            return bool(self._games or self._learned_entries)

    def update_known_games(self, games: tuple[GameRecord, ...]) -> bool:
        """Replace provider evidence and report whether targeting evidence changed."""

        indexed = tuple(
            item
            for game in games
            if game.is_installed
            for item in (_index_game(game),)
            if item is not None
        )
        with self._lock:
            changed = indexed != self._games
            self._games = indexed
            self._last_logged_selection = None
        return changed

    def matches_known_game(self, candidate: FpsTarget) -> bool:
        """Return whether provider or durable learned evidence identifies this process."""

        return self.match(candidate) is not None

    def match(self, candidate: FpsTarget) -> FpsCandidateMatch | None:
        """Return the strongest durable learned or current provider match."""

        with self._lock:
            evidence = _match_candidate(candidate, self._games, self._learned_entries)
        if evidence is None:
            return None
        return _public_match(evidence)

    def record_verified(self, candidate: FpsTarget) -> FpsCandidateMatch | None:
        """Learn an executable only after its current PID produces usable frames."""

        existing_match = self.match(candidate)
        executable_path = candidate.executable_path
        cache = self._learned_cache
        if executable_path is None:
            return existing_match

        identity = existing_match.identity if existing_match is not None else None
        verified_match = existing_match or FpsCandidateMatch(
            identity=None,
            title=PureWindowsPath(executable_path).stem,
            reason="learned-local-executable",
            learned=True,
        )
        if cache is None:
            return existing_match
        try:
            recorded = cache.record(identity, executable_path)
        except OSError:
            _LOGGER.exception("Could not persist learned FPS executable: %s", executable_path)
            return verified_match
        if recorded:
            with self._lock:
                self._learned_entries = cache.entries
                self._last_logged_selection = None
            if identity is None:
                _LOGGER.info("Learned frame-verified local FPS executable: %s", executable_path)
            else:
                _LOGGER.info(
                    "Learned verified FPS executable for %s/%s: %s",
                    identity.provider_id,
                    identity.provider_game_id,
                    executable_path,
                )
        return verified_match

    def select(
        self,
        candidates: tuple[FpsTarget, ...],
        *,
        preferred_target: FpsTarget | None = None,
    ) -> tuple[FpsTarget, ...]:
        with self._lock:
            games = self._games
            learned_entries = self._learned_entries

            unique: list[FpsTarget] = []
            seen_pids: set[int] = set()
            for candidate in candidates:
                if candidate.process_id in seen_pids:
                    continue
                seen_pids.add(candidate.process_id)
                unique.append(candidate)

            preferred_identity = (
                preferred_target.identity_key if preferred_target is not None else None
            )
            scored: list[tuple[int, int, int, FpsTarget, _CandidateEvidence]] = []
            for index, candidate in enumerate(unique):
                evidence = _match_candidate(candidate, games, learned_entries)
                if evidence is None:
                    continue
                is_preferred = candidate.identity_key == preferred_identity
                # Durable frame proof always wins. For unlearned provider matches, the
                # pre-overlay foreground candidate wins before other visible provider games.
                source_rank = 0 if evidence.learned else (1 if is_preferred else 2)
                scored.append((source_rank, evidence.rank, index, candidate, evidence))

            scored.sort(key=lambda item: item[:3])
            selected_rows = scored[: self._max_candidates]
            selected = tuple(item[3] for item in selected_rows)
            signature = tuple(candidate.identity_key for candidate in selected)
            if signature != self._last_logged_selection:
                if selected_rows:
                    summary = ", ".join(
                        f"{candidate.executable_name} "
                        f"(game={evidence.title!r} match={evidence.reason})"
                        for _source, _rank, _index, candidate, evidence in selected_rows
                    )
                    _LOGGER.info("FPS target policy selected: %s", summary)
                else:
                    _LOGGER.debug("FPS target policy found no learned or provider process")
                self._last_logged_selection = signature
            return selected


def _public_match(evidence: _CandidateEvidence) -> FpsCandidateMatch:
    return FpsCandidateMatch(
        identity=evidence.identity,
        title=evidence.title,
        reason=evidence.reason,
        learned=evidence.learned,
    )


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
        identity=game.identity,
        title=game.title,
        executable_path=executable_path,
        executable_name=executable_name,
        install_directory=install_directory,
    )


def _match_candidate(
    candidate: FpsTarget,
    games: tuple[_IndexedGame, ...],
    learned_entries: tuple[LearnedFpsExecutable, ...],
) -> _CandidateEvidence | None:
    candidate_path = (
        _windows_path_key(candidate.executable_path)
        if candidate.executable_path is not None
        else None
    )
    if candidate_path is not None:
        learned = next(
            (
                entry
                for entry in learned_entries
                if _windows_path_key(entry.executable_path) == candidate_path
            ),
            None,
        )
        if learned is not None:
            game = next(
                (game for game in games if game.identity == learned.identity),
                None,
            )
            return _CandidateEvidence(
                rank=0,
                identity=learned.identity,
                title=(
                    game.title
                    if game is not None
                    else PureWindowsPath(learned.executable_path).stem
                ),
                reason=(
                    "learned-provider-executable"
                    if learned.identity is not None
                    else "learned-local-executable"
                ),
                learned=True,
            )

    candidate_name = PureWindowsPath(candidate.executable_name).name.casefold()
    best: _CandidateEvidence | None = None
    for game in games:
        rank: int | None = None
        reason = ""
        if candidate_path is not None and candidate_path == game.executable_path:
            rank = 1
            reason = "exact-executable"
        elif (
            candidate_path is not None
            and game.install_directory is not None
            and _windows_path_is_within(candidate_path, game.install_directory)
        ):
            rank = 2
            reason = "install-directory"
        elif game.executable_name is not None and candidate_name == game.executable_name:
            rank = 3
            reason = "executable-name"
        if rank is None:
            continue
        match = _CandidateEvidence(
            rank=rank,
            identity=game.identity,
            title=game.title,
            reason=reason,
            learned=False,
        )
        if best is None or match.rank < best.rank:
            best = match
            if best.rank == 1:
                break
    return best


def _windows_path_key(path: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in PureWindowsPath(path).parts)


def _windows_path_is_within(
    path: tuple[str, ...],
    root: tuple[str, ...],
) -> bool:
    return len(path) > len(root) and path[: len(root)] == root


__all__ = ["FpsCandidateMatch", "FpsCandidateSelector"]
