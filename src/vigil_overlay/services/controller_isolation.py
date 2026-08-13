"""Fail-safe HidHide controller isolation for focus-preserving overlays.

Vigil normally treats HidHide's shared allow/block lists as user-owned. The narrow
exception is a configuration created from a protected receipt written when Vigil Setup
installs a fresh HidHide. Vigil records that exact clean configuration and may extend
it with new device IDs that HidHide identifies as present gaming input. Existing,
already configured, or later externally changed HidHide installations remain read-only.

Vigil only leases the global hiding switch after proving that configured entries are
either currently verified gaming-device IDs or previously verified IDs retained in
Vigil's exact ownership record, and that the packaged Vigil executable is allow-listed.

The released HidHide API stores the active switch globally, so a minimal helper
process owns each lease.  It restores pass-through when Vigil requests release or
when the Vigil process exits unexpectedly.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import math
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol, cast

from vigil_overlay.core.file_io import atomic_write_text
from vigil_overlay.core.runtime import packaged_executable_path

_LOGGER = logging.getLogger("vigil_overlay")

_CONTROL_DEVICE: Final = r"\\.\HidHide"
_DEVICE_TYPE: Final = 32769
_METHOD_BUFFERED: Final = 0
_FILE_READ_DATA: Final = 1
_GENERIC_READ: Final = 0x80000000
_FILE_SHARE_READ: Final = 0x00000001
_FILE_SHARE_WRITE: Final = 0x00000002
_FILE_SHARE_DELETE: Final = 0x00000004
_OPEN_EXISTING: Final = 3
_FILE_ATTRIBUTE_NORMAL: Final = 0x00000080
_SYNCHRONIZE: Final = 0x00100000
_WAIT_OBJECT_0: Final = 0
_WAIT_TIMEOUT: Final = 258
_CREATE_NO_WINDOW: Final = 0x08000000
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value

_IOCTL_GET_WHITELIST: Final = (
    (_DEVICE_TYPE << 16) | (_FILE_READ_DATA << 14) | (2048 << 2) | _METHOD_BUFFERED
)
_IOCTL_GET_BLACKLIST: Final = (
    (_DEVICE_TYPE << 16) | (_FILE_READ_DATA << 14) | (2050 << 2) | _METHOD_BUFFERED
)
_IOCTL_GET_ACTIVE: Final = (
    (_DEVICE_TYPE << 16) | (_FILE_READ_DATA << 14) | (2052 << 2) | _METHOD_BUFFERED
)
_IOCTL_SET_ACTIVE: Final = (
    (_DEVICE_TYPE << 16) | (_FILE_READ_DATA << 14) | (2053 << 2) | _METHOD_BUFFERED
)
_IOCTL_GET_INVERSE: Final = (
    (_DEVICE_TYPE << 16) | (_FILE_READ_DATA << 14) | (2054 << 2) | _METHOD_BUFFERED
)

_HIDHIDE_REGISTRY_KEY: Final = "SOFTWARE\\Nefarius Software Solutions e.U.\\HidHide"
_LEASE_DIRECTORY_NAME: Final = "controller-isolation"
_LEASE_PREFIX: Final = "lease-"
_LEASE_SUFFIX: Final = ".json"
_HIDHIDE_OWNERSHIP_FILENAME: Final = "hidhide-managed-configuration.json"
_HIDHIDE_OWNERSHIP_SCHEMA: Final = 1
FRESH_HIDHIDE_INSTALL_MARKER: Final = ".vigil-hidhide-fresh-install.json"
_FRESH_HIDHIDE_INSTALL_MARKER_SCHEMA: Final = 1
_FRESH_HIDHIDE_INSTALL_VERSION: Final = "1.5.230"
_FRESH_HIDHIDE_INSTALL_RECEIPT_REGISTRY_KEY: Final = (
    "SOFTWARE\\Vigil Overlay\\Prerequisites\\HidHide"
)
_FRESH_HIDHIDE_INSTALL_RECEIPT_VALUE_NAMES: Final = (
    "Schema",
    "Source",
    "HidHideVersion",
    "State",
)
_MAX_AUTOMATIC_DEVICE_IDS: Final = 32
_LEASE_SCHEMA: Final = 3
_STATUS_SCHEMA: Final = 2
_WATCHDOG_DEVICE_REFRESH_SECONDS: Final = 5.0
_WATCHDOG_HEARTBEAT_TIMEOUT_SECONDS: Final = 15.0
_WATCHDOG_HEARTBEAT_FUTURE_TOLERANCE_SECONDS: Final = 1.0


@dataclass(frozen=True, slots=True)
class HidHideState:
    """The complete read-only HidHide state used by a Vigil lease."""

    active: bool
    inverse: bool
    blocked_device_ids: frozenset[str]
    allowed_application_paths: frozenset[str]

    @property
    def configuration_fingerprint(self) -> str:
        payload = {
            "inverse": self.inverse,
            "blocked_device_ids": sorted(value.casefold() for value in self.blocked_device_ids),
            "allowed_application_paths": sorted(
                value.casefold() for value in self.allowed_application_paths
            ),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ControllerIsolationReadiness:
    """User-facing readiness result for the optional HidHide lease."""

    ready: bool
    detail: str
    configured_device_count: int = 0


class ControllerIsolationPhase(StrEnum):
    """One externally observable phase of the fail-safe HidHide lease."""

    OFF = "off"
    PREPARING = "preparing"
    ACTIVE = "active"
    FAILED_SAFE = "failed_safe"
    FAILED_UNCERTAIN = "failed_uncertain"


@dataclass(frozen=True, slots=True)
class FreshHidHideConfigurationOutcome:
    """Result of consuming a trusted fresh-install configuration marker."""

    configured: bool
    retry_later: bool
    detail: str
    configured_device_count: int = 0


@dataclass(frozen=True, slots=True)
class _ManagedHidHideConfiguration:
    """Persistent proof that Vigil owns one exact clean HidHide configuration."""

    application_full_image_name: str
    configuration_fingerprint: str
    managed_device_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _ManagedHidHideSyncOutcome:
    """One verified synchronization of a Vigil-owned HidHide configuration."""

    state: HidHideState
    verified_device_ids: frozenset[str]
    changed: bool


@dataclass(frozen=True, slots=True)
class _PreparedHidHideLease:
    """Verified pre-activation state produced by the sole watchdog client."""

    readiness: ControllerIsolationReadiness
    state: HidHideState | None = None
    managed_configuration: bool = False


class ControllerIsolationBackend(Protocol):
    """Native operations required by the lease coordinator and watchdog."""

    @property
    def supported(self) -> bool: ...

    @property
    def availability_detail(self) -> str: ...

    @property
    def application_full_image_name(self) -> str | None: ...

    @property
    def trusted_configuration_application_paths(self) -> frozenset[str]: ...

    def snapshot(self) -> HidHideState: ...

    def set_active(self, active: bool) -> None: ...

    def verified_gaming_device_ids(self) -> frozenset[str]: ...

    def configure_fresh_install(self, device_ids: frozenset[str]) -> None: ...

    def add_verified_gaming_devices(self, device_ids: frozenset[str]) -> None: ...

    def configuration_client_path(self) -> Path | None: ...


class UnsupportedControllerIsolationBackend:
    """Portable/read-only fallback that cannot change system input routing."""

    def __init__(self, detail: str = "HidHide controller isolation requires Windows") -> None:
        self.detail = detail

    @property
    def supported(self) -> bool:
        return False

    @property
    def availability_detail(self) -> str:
        return self.detail

    @property
    def application_full_image_name(self) -> str | None:
        return None

    @property
    def trusted_configuration_application_paths(self) -> frozenset[str]:
        return frozenset()

    def snapshot(self) -> HidHideState:
        raise OSError(self.detail)

    def set_active(self, active: bool) -> None:
        del active
        raise OSError(self.detail)

    def verified_gaming_device_ids(self) -> frozenset[str]:
        raise OSError(self.detail)

    def configure_fresh_install(self, device_ids: frozenset[str]) -> None:
        del device_ids
        raise OSError(self.detail)

    def add_verified_gaming_devices(self, device_ids: frozenset[str]) -> None:
        del device_ids
        raise OSError(self.detail)

    def configuration_client_path(self) -> Path | None:
        return None


class WindowsHidHideBackend:
    """Use HidHide's control device plus its official narrow CLI mutation path."""

    def __init__(self, application_path: Path | None = None) -> None:
        if sys.platform != "win32":
            raise OSError("HidHide controller isolation requires Windows")
        self._application_path = application_path or packaged_executable_path()
        self._kernel32 = cast(Any, ctypes.WinDLL("kernel32", use_last_error=True))
        self._configure_api_signatures()
        self._install_root = self._resolve_install_root()
        self._cli_path = self._find_installed_binary("HidHideCLI.exe")
        self._client_path = self._find_installed_binary("HidHideClient.exe")
        self._application_full_image_name = self._resolve_full_image_name(self._application_path)

    @property
    def supported(self) -> bool:
        return (
            self._application_path is not None
            and self._application_full_image_name is not None
            and self._cli_path is not None
        )

    @property
    def availability_detail(self) -> str:
        if self._application_path is None:
            return "Vigil must run from its packaged Windows executable to use HidHide."
        if self._install_root is None:
            return (
                "HidHide's registered install path and official clients were not found. "
                "Restart Windows if Setup just installed HidHide, then rerun Vigil Setup "
                "if it remains unavailable."
            )
        if self._cli_path is None:
            return (
                "HidHide is registered, but HidHideCLI.exe was not found under "
                f"{self._install_root}. Restart Windows, then rerun Vigil Setup if the "
                "client is still missing."
            )
        if self._application_full_image_name is None:
            return (
                "Windows could not resolve VigilOverlay.exe for HidHide's Applications "
                "list. Reinstall Vigil to a local fixed drive."
            )
        return "HidHide's installed driver tools are available to Vigil."

    @property
    def application_full_image_name(self) -> str | None:
        return self._application_full_image_name

    @property
    def trusted_configuration_application_paths(self) -> frozenset[str]:
        resolved: set[str] = set()
        for path in (self._cli_path, self._client_path):
            full_image_name = self._resolve_full_image_name(path)
            if full_image_name is not None:
                resolved.add(full_image_name)
        return frozenset(resolved)

    def snapshot(self) -> HidHideState:
        with self._control_device() as handle:
            return HidHideState(
                active=self._read_bool(handle, _IOCTL_GET_ACTIVE),
                inverse=self._read_bool(handle, _IOCTL_GET_INVERSE),
                blocked_device_ids=frozenset(self._read_multi_string(handle, _IOCTL_GET_BLACKLIST)),
                allowed_application_paths=frozenset(
                    self._read_multi_string(handle, _IOCTL_GET_WHITELIST)
                ),
            )

    def set_active(self, active: bool) -> None:
        value = ctypes.c_ubyte(1 if active else 0)
        returned = wintypes.DWORD(0)
        with self._control_device() as handle:
            succeeded = bool(
                self._kernel32.DeviceIoControl(
                    wintypes.HANDLE(handle),
                    wintypes.DWORD(_IOCTL_SET_ACTIVE),
                    ctypes.byref(value),
                    wintypes.DWORD(ctypes.sizeof(value)),
                    None,
                    0,
                    ctypes.byref(returned),
                    None,
                )
            )
        if not succeeded:
            raise ctypes.WinError(ctypes.get_last_error())

    def verified_gaming_device_ids(self) -> frozenset[str]:
        """Return device IDs from HidHide's own gaming-device enumerator.

        Only connected groups containing at least one gaming HID are accepted.
        Explicit keyboard and mouse child collections are excluded even when they
        share a composite controller container.
        """

        cli_path = self._cli_path
        if cli_path is None:
            raise OSError("The official HidHide command-line client is unavailable")
        completed = subprocess.run(
            (str(cli_path), "--dev-gaming"),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=_CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise OSError(f"HidHide could not enumerate gaming devices: {detail}")
        try:
            groups = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise OSError("HidHide returned invalid gaming-device data") from exc
        if not isinstance(groups, list):
            raise OSError("HidHide returned an invalid gaming-device collection")

        return _verified_ids_from_hidhide_groups(groups)

    def configure_fresh_install(self, device_ids: frozenset[str]) -> None:
        """Extend a fresh configuration while explicitly leaving hiding off."""

        cli_path = self._cli_path
        application_path = self._application_path
        if cli_path is None or application_path is None:
            raise OSError("The official HidHide command-line client is unavailable")
        completed = subprocess.run(
            _fresh_install_cli_command(cli_path, application_path, device_ids),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            creationflags=_CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            if not detail:
                detail = f"exit code {completed.returncode}"
            raise OSError(f"HidHide fresh-install configuration failed: {detail}")

    def add_verified_gaming_devices(self, device_ids: frozenset[str]) -> None:
        """Add verified gaming IDs without changing HidHide's active switch."""

        if not device_ids:
            return
        cli_path = self._cli_path
        if cli_path is None:
            raise OSError("The official HidHide command-line client is unavailable")
        completed = subprocess.run(
            _add_gaming_devices_cli_command(cli_path, device_ids),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            creationflags=_CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            if not detail:
                detail = f"exit code {completed.returncode}"
            raise OSError(f"HidHide controller refresh failed: {detail}")

    def configuration_client_path(self) -> Path | None:
        return self._client_path

    @contextmanager
    def _control_device(self) -> Any:
        ctypes.set_last_error(0)
        handle = int(
            self._kernel32.CreateFileW(
                _CONTROL_DEVICE,
                _GENERIC_READ,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                None,
                _OPEN_EXISTING,
                _FILE_ATTRIBUTE_NORMAL,
                None,
            )
        )
        if handle == _INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            if error == 5:
                raise OSError("HidHide is busy. Close its Configuration Client and try again.")
            if error == 2:
                raise OSError("HidHide is not installed or its driver is unavailable")
            raise ctypes.WinError(error)
        try:
            yield handle
        finally:
            self._kernel32.CloseHandle(wintypes.HANDLE(handle))

    def _read_bool(self, handle: int, control_code: int) -> bool:
        value = ctypes.c_ubyte(0)
        returned = wintypes.DWORD(0)
        succeeded = bool(
            self._kernel32.DeviceIoControl(
                wintypes.HANDLE(handle),
                wintypes.DWORD(control_code),
                None,
                0,
                ctypes.byref(value),
                wintypes.DWORD(ctypes.sizeof(value)),
                ctypes.byref(returned),
                None,
            )
        )
        if not succeeded:
            raise ctypes.WinError(ctypes.get_last_error())
        if returned.value != 1:
            raise OSError("HidHide returned an invalid status response")
        return bool(value.value)

    def _read_multi_string(self, handle: int, control_code: int) -> tuple[str, ...]:
        required = wintypes.DWORD(0)
        succeeded = bool(
            self._kernel32.DeviceIoControl(
                wintypes.HANDLE(handle),
                wintypes.DWORD(control_code),
                None,
                0,
                None,
                0,
                ctypes.byref(required),
                None,
            )
        )
        if not succeeded:
            raise ctypes.WinError(ctypes.get_last_error())
        if required.value == 0 or required.value % ctypes.sizeof(ctypes.c_wchar):
            raise OSError("HidHide returned an invalid list size")
        character_count = required.value // ctypes.sizeof(ctypes.c_wchar)
        buffer = ctypes.create_unicode_buffer(character_count)
        returned = wintypes.DWORD(0)
        succeeded = bool(
            self._kernel32.DeviceIoControl(
                wintypes.HANDLE(handle),
                wintypes.DWORD(control_code),
                None,
                0,
                buffer,
                wintypes.DWORD(ctypes.sizeof(buffer)),
                ctypes.byref(returned),
                None,
            )
        )
        if not succeeded:
            raise ctypes.WinError(ctypes.get_last_error())
        wchar_size = ctypes.sizeof(ctypes.c_wchar)
        if returned.value > ctypes.sizeof(buffer) or returned.value % wchar_size:
            raise OSError("HidHide returned invalid list data")
        raw = ctypes.wstring_at(buffer, returned.value // wchar_size)
        return tuple(value for value in raw.split("\0") if value)

    def _resolve_install_root(self) -> Path | None:
        registered_root: Path | None = None
        try:
            import winreg

            access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                _HIDHIDE_REGISTRY_KEY,
                0,
                access,
            ) as registry_key:
                raw, _kind = winreg.QueryValueEx(registry_key, "Path")
        except (ImportError, OSError):
            raw = None
        if isinstance(raw, str) and raw.strip():
            expanded = os.path.expandvars(raw.strip().strip('"'))
            candidate = Path(expanded).expanduser().resolve()
            registered_root = candidate.parent if candidate.is_file() else candidate

        candidates: list[Path] = []
        if registered_root is not None:
            candidates.append(registered_root)
        for variable in ("ProgramW6432", "ProgramFiles"):
            raw_program_files = os.environ.get(variable)
            if not raw_program_files:
                continue
            program_files = Path(raw_program_files).expanduser().resolve()
            candidates.extend(
                (
                    program_files / "Nefarius Software Solutions" / "HidHide",
                    program_files / "Nefarius Software Solutions e.U." / "HidHide",
                )
            )

        unique: list[Path] = []
        observed: set[str] = set()
        for candidate in candidates:
            candidate_key = str(candidate).casefold()
            if candidate_key in observed:
                continue
            observed.add(candidate_key)
            unique.append(candidate)
        for candidate in unique:
            if any(
                path.is_file() for path in _hidhide_binary_candidates(candidate, "HidHideCLI.exe")
            ):
                return candidate
        return registered_root

    def _find_installed_binary(self, name: str) -> Path | None:
        root = self._install_root
        if root is None:
            return None
        for candidate in _hidhide_binary_candidates(root, name):
            if candidate.is_file():
                return candidate
        return None

    def _resolve_full_image_name(self, path: Path | None) -> str | None:
        if path is None or not path.is_file():
            return None
        resolved = path.resolve()
        drive = resolved.drive
        if not drive:
            return None
        buffer = ctypes.create_unicode_buffer(32_768)
        length = int(self._kernel32.QueryDosDeviceW(drive, buffer, len(buffer)))
        if length <= 0:
            return None
        device_prefix = buffer.value.rstrip("\\")
        relative = str(resolved)[len(drive) :]
        return f"{device_prefix}{relative}"

    def _configure_api_signatures(self) -> None:
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        self._kernel32.DeviceIoControl.restype = wintypes.BOOL
        self._kernel32.QueryDosDeviceW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        self._kernel32.QueryDosDeviceW.restype = wintypes.DWORD
        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD


class ControllerIsolationService:
    """Coordinate one fail-open HidHide lease around the visible overlay."""

    def __init__(
        self,
        backend: ControllerIsolationBackend,
        cache_root: Path,
        *,
        ownership_root: Path | None = None,
        process_launcher: Callable[[Sequence[str]], subprocess.Popen[bytes]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._lease_root = cache_root / _LEASE_DIRECTORY_NAME
        self._ownership_path = (
            (ownership_root or cache_root) / _LEASE_DIRECTORY_NAME / _HIDHIDE_OWNERSHIP_FILENAME
        )
        self._process_launcher = process_launcher or _launch_watchdog_process
        self._clock = clock
        self._lease_path: Path | None = None
        self._watchdog: subprocess.Popen[bytes] | None = None
        self._configuration_fingerprint: str | None = None
        self._managed_configuration = False
        self._phase = ControllerIsolationPhase.OFF
        self._activation_started_at: float | None = None
        self._detail = "Controller isolation is off"

    @property
    def active(self) -> bool:
        return self._phase is ControllerIsolationPhase.ACTIVE

    @property
    def phase(self) -> ControllerIsolationPhase:
        return self._phase

    @property
    def release_required(self) -> bool:
        """Return whether a helper or uncertain global switch still needs release."""

        return self._lease_path is not None

    @property
    def available(self) -> bool:
        """Return whether this installed build can use the HidHide control path."""

        return self._backend.supported

    @property
    def detail(self) -> str:
        return self._detail

    def configure_fresh_install(self) -> FreshHidHideConfigurationOutcome:
        """Configure only an untouched HidHide installed by the current Vigil setup."""

        if not self._backend.supported:
            return FreshHidHideConfigurationOutcome(
                False,
                True,
                self._backend.availability_detail + " Automatic configuration will retry later.",
            )
        try:
            before = self._backend.snapshot()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return FreshHidHideConfigurationOutcome(
                False,
                True,
                f"HidHide could not be inspected yet: {exc}",
            )
        allowed = {value.casefold() for value in before.allowed_application_paths}
        trusted = {
            value.casefold() for value in self._backend.trusted_configuration_application_paths
        }
        if (
            before.active
            or before.inverse
            or before.blocked_device_ids
            or not allowed.issubset(trusted)
        ):
            return FreshHidHideConfigurationOutcome(
                False,
                False,
                "HidHide is already configured; Vigil left its shared settings unchanged.",
            )
        try:
            gaming_ids = self._backend.verified_gaming_device_ids()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return FreshHidHideConfigurationOutcome(
                False,
                True,
                f"HidHide could not enumerate a controller yet: {exc}",
            )
        if not gaming_ids:
            return FreshHidHideConfigurationOutcome(
                False,
                True,
                "Connect a controller and restart Vigil to finish automatic HidHide setup.",
            )
        if len(gaming_ids) > _MAX_AUTOMATIC_DEVICE_IDS:
            return FreshHidHideConfigurationOutcome(
                False,
                False,
                "HidHide reported too many gaming-device paths for safe automatic setup.",
            )
        try:
            self._backend.configure_fresh_install(gaming_ids)
            after = self._backend.snapshot()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self._force_pass_through()
            return FreshHidHideConfigurationOutcome(
                False,
                True,
                f"Automatic HidHide setup did not complete: {exc}",
            )

        application = self._backend.application_full_image_name
        allowed = {value.casefold() for value in after.allowed_application_paths}
        configured = {value.casefold() for value in after.blocked_device_ids}
        expected = {value.casefold() for value in gaming_ids}
        trusted = {
            value.casefold() for value in self._backend.trusted_configuration_application_paths
        }
        if (
            after.active
            or after.inverse
            or application is None
            or application.casefold() not in allowed
            or allowed - {application.casefold()} - trusted
            or configured != expected
        ):
            self._force_pass_through()
            return FreshHidHideConfigurationOutcome(
                False,
                False,
                "Automatic HidHide setup could not be verified; use its Configuration Client.",
            )
        try:
            _write_managed_hidhide_configuration(
                self._ownership_path,
                _ManagedHidHideConfiguration(
                    application_full_image_name=application,
                    configuration_fingerprint=after.configuration_fingerprint,
                    managed_device_ids=after.blocked_device_ids,
                ),
            )
        except OSError as exc:
            return FreshHidHideConfigurationOutcome(
                True,
                False,
                "HidHide was configured safely, but Vigil could not remember ownership "
                f"for future controllers: {exc}",
                len(expected),
            )
        return FreshHidHideConfigurationOutcome(
            True,
            False,
            "HidHide was configured for Vigil; new controllers will be added "
            "automatically and device hiding remains off.",
            len(expected),
        )

    def readiness(self) -> ControllerIsolationReadiness:
        if self.release_required:
            return ControllerIsolationReadiness(
                self._phase is ControllerIsolationPhase.ACTIVE,
                self._detail,
            )
        if not self._backend.supported:
            return ControllerIsolationReadiness(
                False,
                self._backend.availability_detail
                + " Install HidHide with Vigil Setup or the official package, and "
                "restart Windows if requested.",
            )
        try:
            return _prepare_hidhide_lease(self._backend, self._ownership_path).readiness
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return ControllerIsolationReadiness(False, str(exc))

    def begin_activation(self) -> ControllerIsolationPhase:
        """Start a watchdog-owned activation without blocking the caller."""

        if self._phase is ControllerIsolationPhase.PREPARING:
            return self._phase
        if self._phase is ControllerIsolationPhase.ACTIVE:
            return self.poll()
        if self.release_required and not self.deactivate(timeout_seconds=0.5):
            return self._phase
        if not self._backend.supported:
            self._phase = ControllerIsolationPhase.FAILED_UNCERTAIN
            self._detail = (
                self._backend.availability_detail
                + " Install HidHide with Vigil Setup or the official package, and restart "
                "Windows if requested. HidHide pass-through could not be verified."
            )
            return self._phase

        lease_path = self._lease_root / f"{_LEASE_PREFIX}{uuid.uuid4().hex}{_LEASE_SUFFIX}"
        try:
            self._lease_root.mkdir(parents=True, exist_ok=True)
            _write_lease(
                lease_path,
                {
                    "schema": _LEASE_SCHEMA,
                    "owner_pid": os.getpid(),
                    "activation_authorized": False,
                },
            )
            if not _write_status(
                lease_path,
                "preparing",
                "Waiting for the controller-isolation watchdog.",
                clock=self._clock,
            ):
                raise OSError("could not initialize watchdog status")
            watchdog = self._process_launcher(_watchdog_command(lease_path))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            _cleanup_lease_files(lease_path)
            self._phase = ControllerIsolationPhase.FAILED_UNCERTAIN
            self._detail = (
                f"Controller isolation watchdog could not start: {exc}. HidHide "
                "pass-through could not be verified."
            )
            return self._phase

        self._lease_path = lease_path
        self._watchdog = watchdog
        self._configuration_fingerprint = None
        self._managed_configuration = False
        self._activation_started_at = self._clock()
        self._phase = ControllerIsolationPhase.PREPARING
        self._detail = "Preparing controller isolation."
        return self._phase

    def activate(self, *, timeout_seconds: float = 3.0) -> bool:
        """Compatibility wrapper for non-Qt callers that need a bounded wait."""

        phase = self.begin_activation()
        deadline = self._clock() + timeout_seconds
        while phase is ControllerIsolationPhase.PREPARING and self._clock() < deadline:
            time.sleep(0.05)
            phase = self.poll()
        if phase is ControllerIsolationPhase.PREPARING:
            self.cancel_pending_activation()
            self.deactivate(timeout_seconds=min(timeout_seconds, 0.5))
        return self._phase is ControllerIsolationPhase.ACTIVE

    def poll(self) -> ControllerIsolationPhase:
        """Consume the helper's atomic state without opening HidHide's driver."""

        if not self.release_required:
            return self._phase
        lease_path = self._lease_path
        watchdog = self._watchdog
        if lease_path is None:
            self._phase = ControllerIsolationPhase.FAILED_UNCERTAIN
            self._detail = "Controller isolation lease was lost unexpectedly."
            return self._phase

        status = _read_status(_status_path(lease_path))
        status_state = status.get("state")
        now = self._clock()
        if not status:
            return self._recover_failed_watchdog(
                "Controller isolation watchdog did not publish a fresh status."
            )
        if not _status_heartbeat_is_fresh(status, now=now):
            return self._recover_failed_watchdog(
                "Controller isolation watchdog heartbeat became stale."
            )
        if watchdog is None or watchdog.poll() is not None:
            if _status_confirms_pass_through(status):
                detail = str(status.get("detail", "Controller isolation released safely."))
                _cleanup_lease_files(lease_path)
                self._clear_lease_state(phase=ControllerIsolationPhase.FAILED_SAFE)
                self._detail = detail
                return self._phase
            return self._recover_failed_watchdog(
                str(status.get("detail", "Controller isolation watchdog exited unexpectedly."))
            )

        if status_state == "error":
            if _status_confirms_pass_through(status):
                detail = str(status.get("detail", "Controller isolation was not available."))
                self._request_watchdog_release()
                self._stop_watchdog_if_running()
                _cleanup_lease_files(lease_path)
                self._clear_lease_state(phase=ControllerIsolationPhase.FAILED_SAFE)
                self._detail = detail
                return self._phase
            return self._recover_failed_watchdog(
                str(status.get("detail", "Controller isolation watchdog reported an error."))
            )
        if status_state == "released":
            if not _status_confirms_pass_through(status):
                return self._recover_failed_watchdog(
                    "Controller isolation watchdog did not verify pass-through."
                )
            detail = str(status.get("detail", "Controller isolation released safely."))
            self._stop_watchdog_if_running()
            _cleanup_lease_files(lease_path)
            self._clear_lease_state()
            self._detail = detail
            return self._phase
        if status_state in {"preparing", "syncing"}:
            self._detail = str(status.get("detail", "Preparing controller isolation."))
            if self._phase is ControllerIsolationPhase.ACTIVE:
                status_fingerprint = status.get("configuration_fingerprint")
                if status_fingerprint != self._configuration_fingerprint:
                    return self._recover_failed_watchdog(
                        "Controller isolation watchdog reported an invalid refresh state."
                    )
            return self._phase
        if status_state != "active":
            return self._recover_failed_watchdog(
                "Controller isolation watchdog reported an invalid state."
            )

        status_fingerprint = status.get("configuration_fingerprint")
        status_managed = status.get("managed_configuration")
        if not isinstance(status_fingerprint, str) or type(status_managed) is not bool:
            return self._recover_failed_watchdog(
                "Controller isolation watchdog omitted its verified configuration."
            )
        if self._phase is ControllerIsolationPhase.PREPARING:
            try:
                journal = _read_lease(lease_path)
                journal_authorized = _lease_activation_authorized(journal)
                journal_fingerprint = _lease_fingerprint(journal)
                journal_managed = _lease_managed_configuration(journal)
            except (OSError, ValueError, json.JSONDecodeError):
                return self._recover_failed_watchdog(
                    "Controller isolation activation journal could not be verified."
                )
            if (
                not journal_authorized
                or journal_fingerprint != status_fingerprint
                or journal_managed != status_managed
            ):
                return self._recover_failed_watchdog(
                    "Controller isolation activation journal did not match watchdog status."
                )
        if (
            self._phase is ControllerIsolationPhase.ACTIVE
            and status_fingerprint != self._configuration_fingerprint
            and not self._managed_configuration
        ):
            return self._recover_failed_watchdog(
                "HidHide configuration changed during controller isolation."
            )
        self._configuration_fingerprint = status_fingerprint
        self._managed_configuration = status_managed
        self._phase = ControllerIsolationPhase.ACTIVE
        self._detail = str(status.get("detail", "Controller isolation is active."))
        return self._phase

    def maintain(self) -> bool:
        """Compatibility health predicate for existing non-Qt callers."""

        phase = self.poll()
        return phase not in {ControllerIsolationPhase.FAILED_UNCERTAIN}

    def cancel_pending_activation(self) -> None:
        if self._phase is not ControllerIsolationPhase.PREPARING:
            return
        self._detail = "Cancelling controller isolation preparation."
        self._request_watchdog_release()

    def deactivate(self, *, timeout_seconds: float = 3.0) -> bool:
        lease_path = self._lease_path
        if lease_path is None:
            self._phase = ControllerIsolationPhase.OFF
            return True
        self._request_watchdog_release()
        status = _wait_for_status(
            _status_path(lease_path),
            expected={"released", "error"},
            timeout_seconds=timeout_seconds,
            clock=self._clock,
        )
        released = _status_confirms_pass_through(status) and _status_heartbeat_is_fresh(
            status, now=self._clock()
        )
        if not released:
            # The released HidHide API persists the switch globally. If the helper
            # failed, stop our own process before forcing pass-through so it cannot
            # resume later and turn hiding back on.
            self._stop_watchdog_if_running()
            released = self._force_pass_through()
        if released:
            self._stop_watchdog_if_running()
            self._detail = str(status.get("detail", "Controller isolation released"))
            _cleanup_lease_files(lease_path)
            self._clear_lease_state()
            return True
        self._detail = str(
            status.get(
                "detail",
                "HidHide did not return to pass-through; open its Configuration Client.",
            )
        )
        self._phase = ControllerIsolationPhase.FAILED_UNCERTAIN
        return False

    def stop(self) -> None:
        self.deactivate()

    def configuration_client_path(self) -> Path | None:
        return self._backend.configuration_client_path()

    def _recover_failed_watchdog(self, detail: str) -> ControllerIsolationPhase:
        """Stop the sole driver client, then verify pass-through in this process."""

        lease_path = self._lease_path
        activation_authorized = False
        if lease_path is not None:
            try:
                activation_authorized = _lease_activation_authorized(_read_lease(lease_path))
            except (OSError, ValueError, json.JSONDecodeError):
                # A damaged journal cannot prove that Vigil ever enabled hiding.
                # Preserve the user's pre-existing HidHide state in that case.
                activation_authorized = False
        self._request_watchdog_release()
        self._stop_watchdog_if_running()
        if not activation_authorized:
            if lease_path is not None:
                _cleanup_lease_files(lease_path)
            self._clear_lease_state(phase=ControllerIsolationPhase.FAILED_UNCERTAIN)
            self._detail = (
                f"{detail} Vigil had not authorized device hiding, so the existing "
                "HidHide state was left unchanged and could not be verified."
            )
            return self._phase
        if self._force_pass_through():
            if lease_path is not None:
                _cleanup_lease_files(lease_path)
            self._clear_lease_state(phase=ControllerIsolationPhase.FAILED_SAFE)
            self._detail = detail
            return self._phase
        self._phase = ControllerIsolationPhase.FAILED_UNCERTAIN
        self._detail = (
            f"{detail} HidHide pass-through could not be verified; open its Configuration "
            "Client and turn off Enable device hiding."
        )
        return self._phase

    def _force_pass_through(self) -> bool:
        try:
            if self._backend.snapshot().active:
                self._backend.set_active(False)
            return not self._backend.snapshot().active
        except (OSError, ValueError, subprocess.SubprocessError):
            _LOGGER.exception("Could not force HidHide back to pass-through")
            return False

    def _stop_watchdog_if_running(self, *, timeout_seconds: float = 0.5) -> None:
        watchdog = self._watchdog
        if watchdog is None or watchdog.poll() is not None:
            return
        try:
            watchdog.wait(timeout=timeout_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        with suppress(OSError):
            watchdog.terminate()
        try:
            watchdog.wait(timeout=timeout_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        with suppress(OSError):
            watchdog.kill()
        with suppress(OSError, subprocess.TimeoutExpired):
            watchdog.wait(timeout=timeout_seconds)

    def _request_watchdog_release(self) -> None:
        lease_path = self._lease_path
        if lease_path is None:
            return
        release_path = _release_path(lease_path)
        try:
            atomic_write_text(
                release_path,
                "release\n",
                temporary_suffix=".tmp",
                fsync=True,
            )
        except OSError:
            _LOGGER.exception("Could not request controller-isolation release")

    def _clear_lease_state(
        self,
        *,
        phase: ControllerIsolationPhase = ControllerIsolationPhase.OFF,
    ) -> None:
        watchdog = self._watchdog
        if watchdog is not None:
            with suppress(OSError):
                watchdog.poll()
        self._lease_path = None
        self._watchdog = None
        self._configuration_fingerprint = None
        self._managed_configuration = False
        self._activation_started_at = None
        self._phase = phase


def create_platform_controller_isolation_service(
    cache_root: Path,
    ownership_root: Path | None = None,
) -> ControllerIsolationService:
    """Create the optional HidHide lease service without installing anything."""

    backend: ControllerIsolationBackend
    if sys.platform != "win32":
        backend = UnsupportedControllerIsolationBackend()
    else:
        try:
            backend = WindowsHidHideBackend()
        except OSError as exc:
            backend = UnsupportedControllerIsolationBackend(str(exc))
    _recover_stale_leases(cache_root / _LEASE_DIRECTORY_NAME, backend)
    _LOGGER.info("HidHide readiness: %s", backend.availability_detail)
    return ControllerIsolationService(
        backend,
        cache_root,
        ownership_root=ownership_root,
    )


def _no_fresh_hidhide_install_receipt() -> None:
    return None


def _ignore_fresh_hidhide_install_receipt() -> None:
    return None


def consume_fresh_hidhide_install_marker(
    service: ControllerIsolationService,
    install_root: Path,
    *,
    receipt_reader: Callable[[], object | None] = _no_fresh_hidhide_install_receipt,
    receipt_clearer: Callable[[], None] = _ignore_fresh_hidhide_install_receipt,
) -> FreshHidHideConfigurationOutcome | None:
    """Consume supplied installer proof without touching existing HidHide setups."""

    marker_path = install_root / FRESH_HIDHIDE_INSTALL_MARKER
    marker_payload: object | None = None
    marker_present = False
    try:
        marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
        marker_present = True
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, json.JSONDecodeError):
        marker_present = True

    receipt_payload = receipt_reader()
    if not marker_present and receipt_payload is None:
        return None

    expected_marker = {
        "schema": _FRESH_HIDHIDE_INSTALL_MARKER_SCHEMA,
        "source": "VigilOverlay Setup",
        "hidhide_version": _FRESH_HIDHIDE_INSTALL_VERSION,
    }
    expected_receipt = {
        **expected_marker,
        "state": "pending",
    }
    if marker_payload != expected_marker and receipt_payload != expected_receipt:
        _safe_unlink(marker_path)
        receipt_clearer()
        return FreshHidHideConfigurationOutcome(
            False,
            False,
            "The fresh HidHide installation receipt was not recognized; automatic "
            "setup was skipped.",
        )

    outcome = service.configure_fresh_install()
    if not outcome.retry_later:
        _safe_unlink(marker_path)
        receipt_clearer()
    return outcome


def consume_platform_fresh_hidhide_install_receipt(
    service: ControllerIsolationService,
    install_root: Path,
) -> FreshHidHideConfigurationOutcome | None:
    """Consume Vigil Setup's legacy marker or protected Windows receipt."""

    return consume_fresh_hidhide_install_marker(
        service,
        install_root,
        receipt_reader=_read_fresh_hidhide_install_receipt,
        receipt_clearer=_clear_fresh_hidhide_install_receipt,
    )


def _read_fresh_hidhide_install_receipt() -> object | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
    try:
        registry_key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _FRESH_HIDHIDE_INSTALL_RECEIPT_REGISTRY_KEY,
            0,
            access,
        )
    except FileNotFoundError:
        return None
    try:
        with registry_key:
            schema, _schema_kind = winreg.QueryValueEx(registry_key, "Schema")
            source, _source_kind = winreg.QueryValueEx(registry_key, "Source")
            version, _version_kind = winreg.QueryValueEx(registry_key, "HidHideVersion")
            state, _state_kind = winreg.QueryValueEx(registry_key, "State")
    except OSError as exc:
        _LOGGER.warning("Could not read the complete HidHide install receipt: %s", exc)
        return {}
    return {
        "schema": schema,
        "source": source,
        "hidhide_version": version,
        "state": state,
    }


def _clear_fresh_hidhide_install_receipt() -> None:
    if sys.platform != "win32":
        return
    try:
        import winreg
    except ImportError:
        return

    access = winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _FRESH_HIDHIDE_INSTALL_RECEIPT_REGISTRY_KEY,
            0,
            access,
        ) as registry_key:
            for value_name in _FRESH_HIDHIDE_INSTALL_RECEIPT_VALUE_NAMES:
                with suppress(FileNotFoundError):
                    winreg.DeleteValue(registry_key, value_name)
        winreg.DeleteKeyEx(
            winreg.HKEY_LOCAL_MACHINE,
            _FRESH_HIDHIDE_INSTALL_RECEIPT_REGISTRY_KEY,
            winreg.KEY_WOW64_64KEY,
            0,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        _LOGGER.warning("Could not clear the consumed HidHide install receipt: %s", exc)


def _write_managed_hidhide_configuration(
    path: Path,
    configuration: _ManagedHidHideConfiguration,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": _HIDHIDE_OWNERSHIP_SCHEMA,
        "application_full_image_name": configuration.application_full_image_name,
        "configuration_fingerprint": configuration.configuration_fingerprint,
        "managed_device_ids": sorted(
            configuration.managed_device_ids,
            key=str.casefold,
        ),
    }
    atomic_write_text(
        path,
        json.dumps(payload, sort_keys=True) + "\n",
        temporary_suffix=".tmp",
        fsync=True,
    )


def _read_managed_hidhide_configuration(
    path: Path,
) -> _ManagedHidHideConfiguration | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError:
        raise
    except (UnicodeError, json.JSONDecodeError):
        _safe_unlink(path)
        _LOGGER.warning("Discarded an invalid Vigil HidHide ownership record")
        return None
    if not isinstance(payload, dict) or payload.get("schema") != _HIDHIDE_OWNERSHIP_SCHEMA:
        _safe_unlink(path)
        _LOGGER.warning("Discarded an unsupported Vigil HidHide ownership record")
        return None
    application = payload.get("application_full_image_name")
    fingerprint = payload.get("configuration_fingerprint")
    raw_device_ids = payload.get("managed_device_ids")
    if (
        not isinstance(application, str)
        or not application.strip()
        or not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(value not in "0123456789abcdef" for value in fingerprint.casefold())
        or not isinstance(raw_device_ids, list)
    ):
        _safe_unlink(path)
        _LOGGER.warning("Discarded a malformed Vigil HidHide ownership record")
        return None
    normalized = [_normalized_device_id(value) for value in raw_device_ids]
    if (
        not normalized
        or any(value is None for value in normalized)
        or len(normalized) > _MAX_AUTOMATIC_DEVICE_IDS
    ):
        _safe_unlink(path)
        _LOGGER.warning("Discarded an unsafe Vigil HidHide ownership record")
        return None
    device_ids = frozenset(cast(str, value) for value in normalized)
    if len({value.casefold() for value in device_ids}) != len(normalized):
        _safe_unlink(path)
        _LOGGER.warning("Discarded a duplicate Vigil HidHide ownership record")
        return None
    return _ManagedHidHideConfiguration(
        application_full_image_name=application,
        configuration_fingerprint=fingerprint.casefold(),
        managed_device_ids=device_ids,
    )


def _managed_hidhide_configuration_matches(
    backend: ControllerIsolationBackend,
    configuration: _ManagedHidHideConfiguration,
    state: HidHideState,
) -> bool:
    application = backend.application_full_image_name
    if (
        application is None
        or application.casefold() != configuration.application_full_image_name.casefold()
        or state.inverse
        or state.configuration_fingerprint != configuration.configuration_fingerprint
    ):
        return False
    blocked = {value.casefold() for value in state.blocked_device_ids}
    managed = {value.casefold() for value in configuration.managed_device_ids}
    allowed = {value.casefold() for value in state.allowed_application_paths}
    trusted = {value.casefold() for value in backend.trusted_configuration_application_paths}
    return (
        blocked == managed
        and application.casefold() in allowed
        and not (allowed - {application.casefold()} - trusted)
    )


def _state_matches_managed_hidhide_configuration(
    backend: ControllerIsolationBackend,
    ownership_path: Path,
    state: HidHideState,
) -> bool:
    configuration = _read_managed_hidhide_configuration(ownership_path)
    return configuration is not None and _managed_hidhide_configuration_matches(
        backend,
        configuration,
        state,
    )


def _synchronize_owned_hidhide_configuration(
    backend: ControllerIsolationBackend,
    ownership_path: Path,
    *,
    require_managed: bool = False,
    heartbeat: Callable[[str], None] | None = None,
) -> _ManagedHidHideSyncOutcome | None:
    configuration = _read_managed_hidhide_configuration(ownership_path)
    if configuration is None:
        if require_managed:
            raise OSError("Vigil's HidHide ownership record is unavailable")
        return None
    if heartbeat is not None:
        heartbeat("Verifying Vigil's managed HidHide configuration.")
    before = backend.snapshot()
    if not _managed_hidhide_configuration_matches(backend, configuration, before):
        _safe_unlink(ownership_path)
        if require_managed:
            raise OSError(
                "HidHide's shared configuration changed outside Vigil; automatic "
                "controller management stopped"
            )
        _LOGGER.warning("Relinquished automatic HidHide management after shared settings changed")
        return None

    if heartbeat is not None:
        heartbeat("Checking for connected gaming controllers.")
    verified = backend.verified_gaming_device_ids()
    known = {value.casefold() for value in configuration.managed_device_ids}
    additions = frozenset(value for value in verified if value.casefold() not in known)
    if not additions:
        return _ManagedHidHideSyncOutcome(before, verified, False)
    expected_device_ids = configuration.managed_device_ids | additions
    if len({value.casefold() for value in expected_device_ids}) > _MAX_AUTOMATIC_DEVICE_IDS:
        raise OSError("HidHide reported too many gaming-device paths for safe automatic refresh")

    if heartbeat is not None:
        heartbeat("Adding a newly connected verified gaming controller.")
    backend.add_verified_gaming_devices(additions)
    if heartbeat is not None:
        heartbeat("Verifying the refreshed HidHide configuration.")
    after = backend.snapshot()
    expected = {value.casefold() for value in expected_device_ids}
    configured = {value.casefold() for value in after.blocked_device_ids}
    if (
        after.active != before.active
        or after.inverse
        or configured != expected
        or {value.casefold() for value in after.allowed_application_paths}
        != {value.casefold() for value in before.allowed_application_paths}
    ):
        _safe_unlink(ownership_path)
        raise OSError(
            "HidHide's controller refresh could not be verified; automatic management stopped"
        )
    next_configuration = _ManagedHidHideConfiguration(
        application_full_image_name=configuration.application_full_image_name,
        configuration_fingerprint=after.configuration_fingerprint,
        managed_device_ids=after.blocked_device_ids,
    )
    if not _managed_hidhide_configuration_matches(backend, next_configuration, after):
        _safe_unlink(ownership_path)
        raise OSError("HidHide's refreshed configuration is outside Vigil's safety boundary")
    try:
        _write_managed_hidhide_configuration(ownership_path, next_configuration)
    except OSError:
        _safe_unlink(ownership_path)
        raise
    return _ManagedHidHideSyncOutcome(after, verified, True)


def _prepare_hidhide_lease(
    backend: ControllerIsolationBackend,
    ownership_path: Path,
    *,
    heartbeat: Callable[[str], None] | None = None,
) -> _PreparedHidHideLease:
    """Validate one inactive lease while preserving user-owned configurations."""

    managed_sync = _synchronize_owned_hidhide_configuration(
        backend,
        ownership_path,
        heartbeat=heartbeat,
    )
    if managed_sync is None:
        if heartbeat is not None:
            heartbeat("Inspecting HidHide's shared configuration.")
        state = backend.snapshot()
        if heartbeat is not None:
            heartbeat("Checking configured gaming controllers.")
        gaming_ids = backend.verified_gaming_device_ids()
    else:
        state = managed_sync.state
        gaming_ids = managed_sync.verified_device_ids

    if state.active:
        readiness = ControllerIsolationReadiness(
            False,
            "Turn off 'Enable device hiding' in HidHide before Vigil takes a lease.",
        )
    elif state.inverse:
        readiness = ControllerIsolationReadiness(
            False,
            "HidHide inverse application mode is not safe for Vigil controller isolation.",
        )
    elif not state.blocked_device_ids:
        readiness = ControllerIsolationReadiness(
            False,
            "Select only the controller to hide in the official HidHide client.",
        )
    else:
        application = backend.application_full_image_name
        allowed = {value.casefold() for value in state.allowed_application_paths}
        configured = {value.casefold() for value in state.blocked_device_ids}
        verified = {value.casefold() for value in gaming_ids}
        unverified = sorted(configured - verified)
        if application is None or application.casefold() not in allowed:
            readiness = ControllerIsolationReadiness(
                False,
                "Add the installed VigilOverlay.exe to HidHide's Applications list.",
            )
        elif unverified and managed_sync is None:
            readiness = ControllerIsolationReadiness(
                False,
                "HidHide includes an absent or non-gaming input. Remove it before enabling "
                "Vigil isolation.",
            )
        else:
            readiness = ControllerIsolationReadiness(
                True,
                (
                    "HidHide is safely managed by Vigil; new controllers are added automatically."
                    if managed_sync is not None
                    else "HidHide is safely configured for focus-preserving controller isolation."
                ),
                len(configured),
            )
    return _PreparedHidHideLease(
        readiness,
        state,
        managed_configuration=managed_sync is not None,
    )


def run_controller_isolation_watchdog(
    lease_path: Path,
    cache_root: Path,
    ownership_root: Path | None = None,
) -> int:
    """Prepare, own, verify, and release one HidHide lease."""

    expected_root = (cache_root / _LEASE_DIRECTORY_NAME).resolve()
    resolved_lease = lease_path.expanduser().resolve()
    if (
        resolved_lease.parent != expected_root
        or not resolved_lease.name.startswith(_LEASE_PREFIX)
        or resolved_lease.suffix != _LEASE_SUFFIX
    ):
        return 2
    try:
        payload = _read_lease(resolved_lease)
        owner_pid = _lease_owner_pid(payload)
        backend = WindowsHidHideBackend()
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        _write_status(
            resolved_lease,
            "error",
            f"Invalid isolation lease: {exc}",
            pass_through_verified=False,
        )
        return 1

    kernel32 = backend._kernel32
    owner_handle = kernel32.OpenProcess(_SYNCHRONIZE, False, owner_pid)
    if not owner_handle:
        if _lease_activation_authorized(payload):
            try:
                recovery_fingerprint = _lease_fingerprint(payload)
            except ValueError:
                _write_status(
                    resolved_lease,
                    "error",
                    "Could not recover an authorized controller-isolation lease.",
                    pass_through_verified=False,
                )
                return 1
            _restore_pass_through(backend, resolved_lease, recovery_fingerprint)
        else:
            _write_status(
                resolved_lease,
                "released",
                "Controller isolation preparation ended before activation.",
                pass_through_verified=True,
            )
        return 0

    fingerprint: str | None = None
    managed_configuration = False
    activation_authorized = _lease_activation_authorized(payload)
    try:
        ownership_path = (
            (ownership_root or cache_root) / _LEASE_DIRECTORY_NAME / _HIDHIDE_OWNERSHIP_FILENAME
        )

        if payload.get("schema") == _LEASE_SCHEMA:
            _write_status(
                resolved_lease,
                "preparing",
                "Preparing controller isolation.",
            )

            def preparation_heartbeat(detail: str) -> None:
                _write_status(resolved_lease, "preparing", detail)

            prepared = _prepare_hidhide_lease(
                backend,
                ownership_path,
                heartbeat=preparation_heartbeat,
            )
            state = prepared.state
            if state is None or not prepared.readiness.ready:
                _write_status(
                    resolved_lease,
                    "error",
                    prepared.readiness.detail,
                    pass_through_verified=bool(state is not None and not state.active),
                )
                return 1
            fingerprint = state.configuration_fingerprint
            managed_configuration = prepared.managed_configuration
            if _release_path(resolved_lease).exists():
                _write_status(
                    resolved_lease,
                    "released",
                    "Controller isolation preparation was cancelled safely.",
                    pass_through_verified=True,
                )
                return 0
            payload = {
                "schema": _LEASE_SCHEMA,
                "owner_pid": owner_pid,
                "activation_authorized": True,
                "configuration_fingerprint": fingerprint,
                "managed_configuration": managed_configuration,
            }
            _write_lease(resolved_lease, payload)
            activation_authorized = True
            if _release_path(resolved_lease).exists():
                _write_status(
                    resolved_lease,
                    "released",
                    "Controller isolation preparation was cancelled safely.",
                    configuration_fingerprint=fingerprint,
                    managed_configuration=managed_configuration,
                    pass_through_verified=True,
                )
                return 0
        else:
            fingerprint = _lease_fingerprint(payload)
            managed_configuration = _lease_managed_configuration(payload)
            state = backend.snapshot()
            if state.active:
                _write_status(
                    resolved_lease,
                    "error",
                    "HidHide was already active; Vigil refused to take ownership.",
                    pass_through_verified=False,
                )
                return 1
            if state.configuration_fingerprint != fingerprint:
                _write_status(
                    resolved_lease,
                    "error",
                    "HidHide configuration changed before controller isolation started.",
                    pass_through_verified=True,
                )
                return 1
            if managed_configuration and not _state_matches_managed_hidhide_configuration(
                backend,
                ownership_path,
                state,
            ):
                _write_status(
                    resolved_lease,
                    "error",
                    "Vigil's managed HidHide configuration could not be verified.",
                    pass_through_verified=True,
                )
                return 1

        assert fingerprint is not None
        _write_status(
            resolved_lease,
            "preparing",
            "Enabling verified controller isolation.",
            configuration_fingerprint=fingerprint,
            managed_configuration=managed_configuration,
        )
        if _release_path(resolved_lease).exists():
            _write_status(
                resolved_lease,
                "released",
                "Controller isolation preparation was cancelled safely.",
                configuration_fingerprint=fingerprint,
                managed_configuration=managed_configuration,
                pass_through_verified=True,
            )
            return 0
        backend.set_active(True)
        active_state = backend.snapshot()
        if not active_state.active or active_state.configuration_fingerprint != fingerprint:
            with suppress(OSError):
                backend.set_active(False)
            _write_status(
                resolved_lease,
                "error",
                "HidHide could not verify the requested controller-only lease.",
                pass_through_verified=not backend.snapshot().active,
            )
            return 1
        _write_status(
            resolved_lease,
            "active",
            "Controller isolated; the game remains the foreground window.",
            configuration_fingerprint=fingerprint,
            managed_configuration=managed_configuration,
        )

        next_device_refresh = time.monotonic() + _WATCHDOG_DEVICE_REFRESH_SECONDS
        failure_detail: str | None = None
        while True:
            if _release_path(resolved_lease).exists():
                break
            wait_result = int(kernel32.WaitForSingleObject(owner_handle, 200))
            if wait_result == _WAIT_OBJECT_0:
                break
            if wait_result != _WAIT_TIMEOUT:
                break
            if time.monotonic() < next_device_refresh:
                continue
            next_device_refresh = time.monotonic() + _WATCHDOG_DEVICE_REFRESH_SECONDS
            try:

                def refresh_heartbeat(
                    detail: str,
                    current_fingerprint: str = cast(str, fingerprint),
                ) -> None:
                    _write_status(
                        resolved_lease,
                        "syncing",
                        detail,
                        configuration_fingerprint=current_fingerprint,
                        managed_configuration=managed_configuration,
                    )

                refresh_heartbeat("Checking controller-isolation health.")
                refreshed_state, changed = _refresh_watchdog_hidhide_lease(
                    backend,
                    ownership_path,
                    managed_configuration=managed_configuration,
                    expected_fingerprint=fingerprint,
                    heartbeat=refresh_heartbeat,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                failure_detail = f"Controller isolation watchdog verification failed: {exc}"
                break
            fingerprint = refreshed_state.configuration_fingerprint
            _write_status(
                resolved_lease,
                "active",
                (
                    "Controller isolation updated for a newly connected controller."
                    if changed
                    else (
                        "Controller isolated; watching for newly connected controllers."
                        if managed_configuration
                        else "Controller isolated; watchdog verification is active."
                    )
                ),
                configuration_fingerprint=fingerprint,
                managed_configuration=managed_configuration,
            )
        released = _restore_pass_through(
            backend,
            resolved_lease,
            fingerprint,
            publish_status=failure_detail is None,
        )
        if failure_detail is not None:
            _write_status(
                resolved_lease,
                "error",
                failure_detail,
                configuration_fingerprint=fingerprint,
                managed_configuration=managed_configuration,
                pass_through_verified=released,
            )
            return 1
        return 0 if released else 1
    except Exception as exc:
        pass_through_verified = False
        if activation_authorized:
            try:
                if backend.snapshot().active:
                    backend.set_active(False)
                pass_through_verified = not backend.snapshot().active
            except (OSError, ValueError, subprocess.SubprocessError):
                pass_through_verified = False
        else:
            try:
                pass_through_verified = not backend.snapshot().active
            except (OSError, ValueError, subprocess.SubprocessError):
                pass_through_verified = False
        _write_status(
            resolved_lease,
            "error",
            f"Controller isolation failed: {exc}",
            configuration_fingerprint=fingerprint,
            managed_configuration=managed_configuration,
            pass_through_verified=pass_through_verified,
        )
        return 1
    finally:
        kernel32.CloseHandle(owner_handle)


def _refresh_watchdog_hidhide_lease(
    backend: ControllerIsolationBackend,
    ownership_path: Path,
    *,
    managed_configuration: bool,
    expected_fingerprint: str,
    heartbeat: Callable[[str], None] | None = None,
) -> tuple[HidHideState, bool]:
    """Verify one active lease without introducing a second HidHide client."""

    if managed_configuration:
        sync = _synchronize_owned_hidhide_configuration(
            backend,
            ownership_path,
            require_managed=True,
            heartbeat=heartbeat,
        )
        if sync is None:
            raise OSError("Vigil's managed HidHide configuration was lost")
        state = sync.state
        changed = sync.changed
    else:
        if heartbeat is not None:
            heartbeat("Verifying controller-isolation configuration.")
        state = backend.snapshot()
        changed = False
        if state.configuration_fingerprint != expected_fingerprint:
            raise OSError("HidHide configuration changed during controller isolation")
    if not state.active:
        raise OSError("HidHide controller isolation was turned off unexpectedly")
    return state, changed


def _restore_pass_through(
    backend: ControllerIsolationBackend,
    lease_path: Path,
    fingerprint: str,
    *,
    publish_status: bool = True,
) -> bool:
    try:
        before = backend.snapshot()
        if before.configuration_fingerprint != fingerprint:
            _LOGGER.warning(
                "HidHide configuration changed during a Vigil lease; forcing pass-through"
            )
        if before.active:
            backend.set_active(False)
        after = backend.snapshot()
        if after.active:
            raise OSError("HidHide still reports device hiding active")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if publish_status:
            _write_status(
                lease_path,
                "error",
                f"Could not restore HidHide pass-through: {exc}",
                pass_through_verified=False,
            )
        return False
    if publish_status:
        _write_status(
            lease_path,
            "released",
            "Controller isolation released safely.",
            pass_through_verified=True,
        )
    return True


def _recover_stale_leases(
    lease_root: Path,
    backend: ControllerIsolationBackend,
) -> None:
    if not backend.supported or not lease_root.is_dir():
        return
    for lease_path in lease_root.glob(f"{_LEASE_PREFIX}*{_LEASE_SUFFIX}"):
        try:
            payload = _read_lease(lease_path)
            owner_pid = _lease_owner_pid(payload)
            activation_authorized = _lease_activation_authorized(payload)
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            _cleanup_lease_files(lease_path)
            continue
        if _process_is_running(owner_pid):
            continue
        if not activation_authorized:
            _cleanup_lease_files(lease_path)
            _LOGGER.warning(
                "Discarded incomplete controller-isolation preparation from PID %d",
                owner_pid,
            )
            continue
        try:
            state = backend.snapshot()
            if state.active:
                backend.set_active(False)
            if backend.snapshot().active:
                raise OSError("HidHide still reports device hiding active")
        except (OSError, ValueError, subprocess.SubprocessError):
            _LOGGER.exception("Could not recover a stale controller-isolation lease")
            continue
        _cleanup_lease_files(lease_path)
        _LOGGER.warning("Recovered stale HidHide controller isolation from PID %d", owner_pid)


def _process_is_running(process_id: int) -> bool:
    if sys.platform != "win32" or process_id <= 0:
        return False
    try:
        kernel32 = cast(Any, ctypes.WinDLL("kernel32", use_last_error=True))
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(_SYNCHRONIZE, False, process_id)
    except (AttributeError, OSError):
        return False
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _watchdog_command(lease_path: Path) -> tuple[str, ...]:
    packaged = packaged_executable_path()
    if packaged is not None:
        return (str(packaged), "--controller-isolation-watchdog", str(lease_path))
    return (
        str(Path(sys.executable).resolve()),
        "-m",
        "vigil_overlay",
        "--controller-isolation-watchdog",
        str(lease_path),
    )


def _launch_watchdog_process(command: Sequence[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        tuple(command),
        close_fds=True,
        creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def _wait_for_status(
    path: Path,
    *,
    expected: set[str],
    timeout_seconds: float,
    clock: Callable[[], float],
) -> dict[str, object]:
    deadline = clock() + timeout_seconds
    while clock() < deadline:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
            continue
        if isinstance(payload, dict) and payload.get("state") in expected:
            return payload
        time.sleep(0.05)
    return {"state": "timeout", "detail": "Controller isolation watchdog timed out."}


def _write_status(
    lease_path: Path,
    state: str,
    detail: str,
    *,
    configuration_fingerprint: str | None = None,
    managed_configuration: bool | None = None,
    pass_through_verified: bool | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    payload: dict[str, object] = {
        "schema": _STATUS_SCHEMA,
        "state": state,
        "detail": detail,
        "heartbeat_monotonic": clock(),
    }
    if configuration_fingerprint is not None:
        payload["configuration_fingerprint"] = configuration_fingerprint
    if managed_configuration is not None:
        payload["managed_configuration"] = managed_configuration
    if pass_through_verified is not None:
        payload["pass_through_verified"] = pass_through_verified
    try:
        atomic_write_text(
            _status_path(lease_path),
            json.dumps(payload, sort_keys=True) + "\n",
            temporary_suffix=".tmp",
            fsync=True,
        )
    except OSError:
        _LOGGER.exception("Could not write controller-isolation watchdog status")
        return False
    return True


def _read_status(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_lease(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") not in {1, 2, 3}:
        raise ValueError("unsupported controller-isolation lease")
    return payload


def _write_lease(path: Path, payload: dict[str, object]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, sort_keys=True) + "\n",
        temporary_suffix=".tmp",
        fsync=True,
    )


def _lease_activation_authorized(payload: dict[str, object]) -> bool:
    if payload.get("schema") in {1, 2}:
        # Legacy leases were written only after the complete configuration was
        # verified, immediately before the watchdog enabled hiding.
        return True
    value = payload.get("activation_authorized")
    if type(value) is not bool:
        raise ValueError("invalid controller-isolation activation journal")
    return value


def _status_confirms_pass_through(status: dict[str, object]) -> bool:
    return status.get("pass_through_verified") is True


def _status_heartbeat_is_fresh(
    status: dict[str, object],
    *,
    now: float,
) -> bool:
    if status.get("schema") != _STATUS_SCHEMA:
        return False
    heartbeat = status.get("heartbeat_monotonic")
    if isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float)):
        return False
    heartbeat_value = float(heartbeat)
    if not math.isfinite(heartbeat_value):
        return False
    age = now - heartbeat_value
    return (
        -_WATCHDOG_HEARTBEAT_FUTURE_TOLERANCE_SECONDS <= age <= _WATCHDOG_HEARTBEAT_TIMEOUT_SECONDS
    )


def _lease_owner_pid(payload: dict[str, object]) -> int:
    value = payload.get("owner_pid")
    if type(value) is not int or value <= 0:
        raise ValueError("invalid controller-isolation owner PID")
    return value


def _lease_fingerprint(payload: dict[str, object]) -> str:
    value = payload.get("configuration_fingerprint")
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("invalid controller-isolation configuration fingerprint")
    return value


def _lease_managed_configuration(payload: dict[str, object]) -> bool:
    value = payload.get("managed_configuration", False)
    if type(value) is not bool:
        raise ValueError("invalid managed-configuration lease flag")
    return value


def _status_path(lease_path: Path) -> Path:
    return lease_path.with_suffix(".status.json")


def _release_path(lease_path: Path) -> Path:
    return lease_path.with_suffix(".release")


def _cleanup_lease_files(lease_path: Path) -> None:
    for path in (lease_path, _status_path(lease_path), _release_path(lease_path)):
        _safe_unlink(path)


def _safe_unlink(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


def _normalized_device_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized else None


def _hidhide_binary_candidates(root: Path, name: str) -> tuple[Path, ...]:
    return (
        root / name,
        root / "x64" / name,
        root / "bin" / name,
        root / "bin" / "x64" / name,
    )


def _fresh_install_cli_command(
    cli_path: Path,
    application_path: Path,
    device_ids: frozenset[str],
) -> tuple[str, ...]:
    command = [
        str(cli_path),
        "--cloak-off",
        "--inv-off",
        "--app-reg",
        str(application_path),
    ]
    for device_id in sorted(device_ids, key=str.casefold):
        command.extend(("--dev-hide", device_id))
    return tuple(command)


def _add_gaming_devices_cli_command(
    cli_path: Path,
    device_ids: frozenset[str],
) -> tuple[str, ...]:
    command = [str(cli_path)]
    for device_id in sorted(device_ids, key=str.casefold):
        command.extend(("--dev-hide", device_id))
    return tuple(command)


def _verified_ids_from_hidhide_groups(groups: list[object]) -> frozenset[str]:
    """Accept only IDs that HidHide itself marks as unambiguously gaming input.

    A child path is accepted only when that exact child is present and marked as a
    gaming device. A base-container or XUSB path is broader, so it is accepted only
    when every HID child in the container is marked as gaming. This intentionally
    rejects composite groups containing keyboard, mouse, keypad, consumer-control,
    or unknown interfaces even if one sibling is a gamepad.
    """

    verified: set[str] = set()
    for raw_group in groups:
        if not isinstance(raw_group, dict):
            continue
        raw_devices = raw_group.get("devices")
        if not isinstance(raw_devices, list):
            continue
        devices = [value for value in raw_devices if isinstance(value, dict)]
        present_gaming = [
            value
            for value in devices
            if value.get("present") is True and value.get("gamingDevice") is True
        ]
        if not present_gaming:
            continue
        for value in present_gaming:
            child_id = _normalized_device_id(value.get("deviceInstancePath"))
            if child_id:
                verified.add(child_id)
        if not devices or not all(value.get("gamingDevice") is True for value in devices):
            continue
        for value in devices:
            for key in (
                "baseContainerDeviceInstancePath",
                "xusbDeviceInstancePath",
            ):
                related_id = _normalized_device_id(value.get(key))
                if related_id:
                    verified.add(related_id)
    return frozenset(verified)


__all__ = [
    "FRESH_HIDHIDE_INSTALL_MARKER",
    "ControllerIsolationBackend",
    "ControllerIsolationPhase",
    "ControllerIsolationReadiness",
    "ControllerIsolationService",
    "FreshHidHideConfigurationOutcome",
    "HidHideState",
    "UnsupportedControllerIsolationBackend",
    "WindowsHidHideBackend",
    "consume_fresh_hidhide_install_marker",
    "consume_platform_fresh_hidhide_install_receipt",
    "create_platform_controller_isolation_service",
    "run_controller_isolation_watchdog",
]
