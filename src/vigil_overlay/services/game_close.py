"""Safe host-owned running-game close requests for Home recent-game rows."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Any, Protocol, cast

from vigil_overlay.contracts.games import GameIdentity, GameLaunchTargetKind, GameRecord


@dataclass(frozen=True, slots=True)
class RunningWindowProcess:
    """One process that currently owns at least one visible top-level window."""

    process_id: int
    executable_path: str


class GameCloseOutcome(StrEnum):
    """Result of one graceful close request."""

    REQUESTED = "requested"
    NOT_RUNNING = "not_running"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class GameCloseResult:
    """Outcome returned to the application after requesting a game close."""

    outcome: GameCloseOutcome
    process_id: int | None = None


class GameCloseBackend(Protocol):
    """Platform boundary used by :class:`GameCloseService`."""

    def visible_processes(self) -> tuple[RunningWindowProcess, ...]: ...

    def request_graceful_close(self, process_id: int) -> bool: ...


class GameCloseService:
    """Resolve a game to one visible process and request a graceful window close.

    Vigil intentionally never falls back to TerminateProcess/kill. A Home close action is
    exposed only when the provider record can be matched to exactly one visible process by
    exact executable path or install-directory containment.
    """

    def __init__(self, backend: GameCloseBackend | None = None) -> None:
        self._backend = backend

    def closable_game_identities(
        self, games: tuple[GameRecord, ...]
    ) -> frozenset[GameIdentity]:
        """Return games that currently resolve to exactly one visible process."""

        backend = self._backend
        if backend is None or not games:
            return frozenset()
        processes = backend.visible_processes()
        return frozenset(
            game.identity
            for game in games
            if _resolve_unique_process(game, processes) is not None
        )

    def request_close(self, game: GameRecord) -> GameCloseResult:
        """Request WM_CLOSE for one confidently matched running game."""

        backend = self._backend
        if backend is None:
            return GameCloseResult(GameCloseOutcome.UNSUPPORTED)
        processes = backend.visible_processes()
        match, ambiguous = _resolve_process(game, processes)
        if ambiguous:
            return GameCloseResult(GameCloseOutcome.AMBIGUOUS)
        if match is None:
            return GameCloseResult(GameCloseOutcome.NOT_RUNNING)
        if not backend.request_graceful_close(match.process_id):
            return GameCloseResult(GameCloseOutcome.NOT_RUNNING)
        return GameCloseResult(GameCloseOutcome.REQUESTED, process_id=match.process_id)


def create_platform_game_close_service() -> GameCloseService:
    """Create the Win32 graceful-close backend when supported by this platform."""

    if os.name != "nt":
        return GameCloseService()
    return GameCloseService(_WindowsGameCloseBackend())


def _resolve_unique_process(
    game: GameRecord,
    processes: tuple[RunningWindowProcess, ...],
) -> RunningWindowProcess | None:
    match, ambiguous = _resolve_process(game, processes)
    return None if ambiguous else match


def _resolve_process(
    game: GameRecord,
    processes: tuple[RunningWindowProcess, ...],
) -> tuple[RunningWindowProcess | None, bool]:
    target = game.launch_target
    if target is not None and target.kind is GameLaunchTargetKind.EXECUTABLE:
        exact = _unique_by_pid(
            process
            for process in processes
            if _windows_path_key(process.executable_path)
            == _windows_path_key(target.target)
        )
        if len(exact) == 1:
            return (exact[0], False)
        if len(exact) > 1:
            return (None, True)

    if game.install_directory is None:
        return (None, False)
    contained = _unique_by_pid(
        process
        for process in processes
        if _windows_path_is_within(process.executable_path, game.install_directory)
    )
    if len(contained) == 1:
        return (contained[0], False)
    if len(contained) > 1:
        return (None, True)
    return (None, False)


def _unique_by_pid(processes: Any) -> tuple[RunningWindowProcess, ...]:
    by_pid: dict[int, RunningWindowProcess] = {}
    for process in processes:
        by_pid.setdefault(process.process_id, process)
    return tuple(by_pid.values())


def _windows_path_key(path: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in PureWindowsPath(path).parts)


def _windows_path_is_within(path: str, root: str) -> bool:
    path_parts = _windows_path_key(path)
    root_parts = _windows_path_key(root)
    return (
        len(path_parts) > len(root_parts)
        and path_parts[: len(root_parts)] == root_parts
    )


class _WindowsGameCloseBackend:
    """Enumerate visible top-level windows and request graceful Win32 WM_CLOSE."""

    _WM_CLOSE = 0x0010
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def visible_processes(self) -> tuple[RunningWindowProcess, ...]:
        user32, kernel32 = self._libraries()
        enum_windows = user32.EnumWindows
        is_visible = user32.IsWindowVisible
        get_pid = user32.GetWindowThreadProcessId
        seen: dict[int, RunningWindowProcess] = {}

        callback_type = cast(Any, ctypes).WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def visit(hwnd: Any, _lparam: Any) -> bool:
            if not bool(is_visible(hwnd)):
                return True
            pid_value = wintypes.DWORD(0)
            get_pid(hwnd, ctypes.byref(pid_value))
            process_id = int(pid_value.value)
            if process_id <= 0 or process_id == os.getpid() or process_id in seen:
                return True
            executable_path = self._query_process_path(kernel32, process_id)
            if executable_path:
                seen[process_id] = RunningWindowProcess(process_id, executable_path)
            return True

        callback = callback_type(visit)
        enum_windows(callback, 0)
        return tuple(seen.values())

    def request_graceful_close(self, process_id: int) -> bool:
        user32, _kernel32 = self._libraries()
        enum_windows = user32.EnumWindows
        is_visible = user32.IsWindowVisible
        get_pid = user32.GetWindowThreadProcessId
        post_message = user32.PostMessageW
        posted = False

        callback_type = cast(Any, ctypes).WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def visit(hwnd: Any, _lparam: Any) -> bool:
            nonlocal posted
            if not bool(is_visible(hwnd)):
                return True
            pid_value = wintypes.DWORD(0)
            get_pid(hwnd, ctypes.byref(pid_value))
            if int(pid_value.value) != process_id:
                return True
            if bool(post_message(hwnd, self._WM_CLOSE, 0, 0)):
                posted = True
            return True

        callback = callback_type(visit)
        enum_windows(callback, 0)
        return posted

    @staticmethod
    def _libraries() -> tuple[Any, Any]:
        windll_type = cast(Any, ctypes).WinDLL
        user32 = windll_type("user32", use_last_error=True)
        kernel32 = windll_type("kernel32", use_last_error=True)
        return (user32, kernel32)

    def _query_process_path(self, kernel32: Any, process_id: int) -> str:
        open_process = kernel32.OpenProcess
        close_handle = kernel32.CloseHandle
        query_name = kernel32.QueryFullProcessImageNameW
        handle = open_process(
            self._PROCESS_QUERY_LIMITED_INFORMATION, False, process_id
        )
        if not handle:
            return ""
        try:
            capacity = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if not query_name(handle, 0, buffer, ctypes.byref(capacity)):
                return ""
            return buffer.value
        finally:
            close_handle(handle)


__all__ = [
    "GameCloseBackend",
    "GameCloseOutcome",
    "GameCloseResult",
    "GameCloseService",
    "RunningWindowProcess",
    "create_platform_game_close_service",
]
