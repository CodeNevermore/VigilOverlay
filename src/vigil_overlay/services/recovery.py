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

    packaged = packaged_executable_path()
    if packaged is not None:
        return (str(packaged), "--safe-mode", "--wait-for-instance-exit")
    return (
        str(Path(sys.executable).resolve()),
        "-m",
        "vigil_overlay",
        "--safe-mode",
        "--wait-for-instance-exit",
    )


def launch_recovery_process(command: Sequence[str]) -> None:
    """Launch a replacement Vigil process without shell command interpretation."""

    subprocess.Popen(tuple(command), close_fds=True)
