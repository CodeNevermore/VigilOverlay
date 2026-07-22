"""Per-user Windows startup registration for the installed Vigil executable."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from vigil_overlay.core.runtime import packaged_executable_path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "VigilOverlay"


class StartupBackend(Protocol):
    """Minimal persistence boundary for one per-user startup command."""

    def read_command(self) -> str | None: ...

    def write_command(self, command: str) -> None: ...

    def remove_command(self) -> None: ...


def _load_winreg() -> Any:
    return importlib.import_module("winreg")


class WindowsRegistryStartupBackend:
    """Store Vigil's startup command in the current user's Windows Run key."""

    def read_command(self) -> str | None:
        winreg = _load_winreg()

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                _RUN_KEY,
                0,
                winreg.KEY_READ,
            ) as key:
                value, value_type = winreg.QueryValueEx(key, _VALUE_NAME)
        except FileNotFoundError:
            return None
        if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} or not isinstance(
            value, str
        ):
            return None
        return value

    def write_command(self, command: str) -> None:
        winreg = _load_winreg()

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, command)

    def remove_command(self) -> None:
        winreg = _load_winreg()

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                _RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.DeleteValue(key, _VALUE_NAME)
        except FileNotFoundError:
            return


@dataclass(frozen=True, slots=True)
class StartupRegistrationState:
    """Observed state for the current installed executable."""

    supported: bool
    enabled: bool
    command: str | None
    detail: str


class StartupRegistrationService:
    """Own startup command normalization, reconciliation, and rollback support."""

    def __init__(
        self,
        backend: StartupBackend | None,
        executable_path: Path | None,
    ) -> None:
        self._backend = backend
        self._executable_path = (
            executable_path.resolve() if executable_path is not None else None
        )

    @property
    def supported(self) -> bool:
        return self._backend is not None and self._executable_path is not None

    @property
    def desired_command(self) -> str | None:
        if self._executable_path is None:
            return None
        return f'"{self._executable_path}"'

    def read_command(self) -> str | None:
        if self._backend is None:
            return None
        return self._backend.read_command()

    def state(self) -> StartupRegistrationState:
        if not self.supported:
            return StartupRegistrationState(
                supported=False,
                enabled=False,
                command=None,
                detail="Start with Windows is available in the installed Windows build.",
            )
        command = self.read_command()
        desired = self.desired_command
        enabled = command == desired
        detail = "Registered" if enabled else "Not registered"
        if command is not None and not enabled:
            detail = "A stale Vigil startup entry was detected"
        return StartupRegistrationState(True, enabled, command, detail)

    def set_enabled(self, enabled: bool) -> StartupRegistrationState:
        if self._backend is None or self._executable_path is None:
            return StartupRegistrationState(
                supported=False,
                enabled=False,
                command=None,
                detail="Start with Windows is unavailable outside the installed Windows build.",
            )
        if enabled:
            command = self.desired_command
            assert command is not None
            self._backend.write_command(command)
        else:
            self._backend.remove_command()
        return self.state()

    def reconcile(self, enabled: bool) -> StartupRegistrationState:
        """Make the registry match the saved preference, repairing stale paths when needed."""

        if not self.supported:
            return self.state()
        current = self.read_command()
        desired = self.desired_command
        if enabled and current != desired:
            assert self._backend is not None
            assert desired is not None
            self._backend.write_command(desired)
        elif not enabled and current is not None:
            assert self._backend is not None
            self._backend.remove_command()
        return self.state()

    def restore_command(self, command: str | None) -> None:
        """Restore an exact pre-change registry snapshot after a failed transaction."""

        if self._backend is None:
            return
        if command is None:
            self._backend.remove_command()
        else:
            self._backend.write_command(command)


def create_platform_startup_service() -> StartupRegistrationService:
    """Create the per-user Windows startup service without making it a source-run dependency."""

    executable = packaged_executable_path()
    if executable is None:
        return StartupRegistrationService(None, None)
    return StartupRegistrationService(WindowsRegistryStartupBackend(), executable)
