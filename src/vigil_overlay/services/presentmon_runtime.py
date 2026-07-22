"""Bundled PresentMon runtime discovery and integrity verification."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Final

from vigil_overlay.core.file_io import sha256_file
from vigil_overlay.core.paths import ApplicationPaths
from vigil_overlay.services.fps import PresentMonRuntime

PRESENTMON_VERSION: Final[str] = "2.5.1"
PRESENTMON_FILENAME: Final[str] = f"PresentMon-{PRESENTMON_VERSION}-x64.exe"
PRESENTMON_SHA256: Final[str] = (
    "9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191"
)


class PresentMonRuntimeError(RuntimeError):
    """The bundled PresentMon runtime is missing, unsupported, or untrusted."""


class PresentMonRuntimeManager:
    """Resolve only Vigil's bundled, checksum-verified PresentMon executable."""

    def __init__(self, paths: ApplicationPaths) -> None:
        self._paths = paths

    @property
    def bundled_executable(self) -> Path:
        return (
            self._paths.resource_root
            / "third_party"
            / "presentmon"
            / "bin"
            / PRESENTMON_FILENAME
        )

    def ensure(self) -> PresentMonRuntime:
        if not _windows_x64_supported():
            machine = platform.machine().lower()
            raise PresentMonRuntimeError(
                f"PresentMon FPS telemetry requires 64-bit Windows (detected {os.name}/{machine})"
            )

        executable = self.bundled_executable
        if not executable.is_file():
            raise PresentMonRuntimeError(
                "The bundled PresentMon runtime is missing from this Vigil installation: "
                f"{executable}"
            )

        actual_sha256 = sha256_file(executable)
        if actual_sha256 != PRESENTMON_SHA256:
            raise PresentMonRuntimeError(
                "The bundled PresentMon runtime failed SHA-256 verification and will not be run"
            )

        return PresentMonRuntime(executable, PRESENTMON_VERSION, PRESENTMON_SHA256)


def _windows_x64_supported() -> bool:
    return os.name == "nt" and platform.machine().lower() in {"amd64", "x86_64"}
