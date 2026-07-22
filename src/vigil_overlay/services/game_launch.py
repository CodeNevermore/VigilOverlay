"""Host-owned validation and execution for provider-declared game launch targets."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence

from vigil_overlay.contracts.games import GameLaunchTargetKind, GameRecord
from vigil_overlay.services.game_library import GameProviderRegistry

UriLauncher = Callable[[str], None]
ProcessLauncher = Callable[[Sequence[str], str | None], None]


class GameLaunchError(RuntimeError):
    """Raised when a provider-declared launch target cannot be safely executed."""


class GameLaunchService:
    """Validate provider launch policy, then delegate execution to the operating system."""

    def __init__(
        self,
        registry: GameProviderRegistry,
        *,
        uri_launcher: UriLauncher | None = None,
        process_launcher: ProcessLauncher | None = None,
    ) -> None:
        self._registry = registry
        self._uri_launcher = uri_launcher or _default_uri_launcher
        self._process_launcher = process_launcher or _default_process_launcher

    def launch(self, game: GameRecord) -> None:
        if not game.is_available or game.launch_target is None:
            raise GameLaunchError("game is not available for launch")
        if not self._registry.validates_launch_target(game):
            raise GameLaunchError(
                "provider launch target is not allowed by host policy"
            )

        target = game.launch_target
        if target.kind is GameLaunchTargetKind.URI:
            self._uri_launcher(target.target)
            return
        if target.kind is GameLaunchTargetKind.EXECUTABLE:
            command = (target.target, *target.arguments)
            self._process_launcher(command, target.working_directory)
            return
        raise GameLaunchError(f"unsupported launch target kind: {target.kind}")


def _default_uri_launcher(uri: str) -> None:
    if sys.platform != "win32":
        raise GameLaunchError("URI game launching is available only on Windows")
    os.startfile(uri)


def _default_process_launcher(
    command: Sequence[str], working_directory: str | None
) -> None:
    if sys.platform != "win32":
        raise GameLaunchError("executable game launching is available only on Windows")
    subprocess.Popen(
        list(command),
        cwd=working_directory,
        close_fds=True,
    )


__all__ = ["GameLaunchError", "GameLaunchService"]
