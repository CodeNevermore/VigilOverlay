"""Process-launch helpers for consumer recovery actions."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from vigil_overlay.core.runtime import packaged_executable_path


class RecoveryProcessLauncher(Protocol):
    """Callable boundary used to launch a replacement Vigil process."""

    def __call__(self, command: Sequence[str]) -> None: ...


def safe_mode_restart_command() -> tuple[str, ...]:
    """Build a Safe Mode restart command for packaged and source runs."""

    return _restart_command(safe_mode=True)


def normal_mode_restart_command() -> tuple[str, ...]:
    """Leave Safe Mode by restarting with saved settings and fresh FPS state."""

    return _restart_command(safe_mode=False)


def _restart_command(*, safe_mode: bool) -> tuple[str, ...]:
    packaged = packaged_executable_path()
    entrypoint = (
        (str(packaged),)
        if packaged is not None
        else (str(Path(sys.executable).resolve()), "-m", "vigil_overlay")
    )
    mode = ("--safe-mode",) if safe_mode else ()
    return (*entrypoint, *mode, "--wait-for-instance-exit", "--reset-fps-sessions")


def launch_recovery_process(command: Sequence[str]) -> None:
    """Launch a replacement Vigil process without shell command interpretation."""

    subprocess.Popen(tuple(command), close_fds=True)
