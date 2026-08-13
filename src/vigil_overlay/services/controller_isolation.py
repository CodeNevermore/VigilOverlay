"""One-release retirement recovery for Vigil's former HidHide integration.

Vigil 0.1.4.0 no longer installs or uses HidHide for active controller handling.  This
module exists only so an upgrade from a prior Vigil build can turn off a verified Vigil-authored
lease before the active integration is removed.  User-owned HidHide configuration and
the installed driver are never removed or rewritten.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_LOGGER = logging.getLogger(__name__)

_LEASE_DIRECTORY_NAME: Final = "controller-isolation"
_LEASE_PREFIX: Final = "lease-"
_LEASE_SUFFIX: Final = ".json"
_OWNERSHIP_FILENAME: Final = "hidhide-managed-configuration.json"
_FRESH_INSTALL_MARKER: Final = ".vigil-hidhide-fresh-install.json"
_RECEIPT_KEY: Final = r"SOFTWARE\Vigil Overlay\Prerequisites\HidHide"
_CREATE_NO_WINDOW: Final = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True, slots=True)
class HidHideRetirementOutcome:
    """Result of retiring only Vigil-authored legacy HidHide state."""

    pass_through_verified: bool
    detail: str
    recovered_lease_count: int = 0

    @property
    def warning_required(self) -> bool:
        return not self.pass_through_verified


def retire_platform_hidhide_state(
    cache_root: Path,
    user_data_root: Path,
    install_root: Path,
) -> HidHideRetirementOutcome:
    """Restore pass-through for old Vigil leases, then remove only Vigil metadata.

    A malformed or unsupported journal is preserved and reported instead of assuming
    that hiding is safe to leave alone.  PID liveness is deliberately ignored: the new
    Vigil instance has already acquired the single-instance guard, and Windows can
    reuse PIDs after an unclean exit.
    """

    lease_root = cache_root / _LEASE_DIRECTORY_NAME
    lease_paths = tuple(
        path
        for path in sorted(lease_root.glob(f"{_LEASE_PREFIX}*{_LEASE_SUFFIX}"))
        if not path.name.endswith(".status.json")
    )
    authorized: list[Path] = []
    incomplete: list[Path] = []
    uncertain: list[Path] = []

    for lease_path in lease_paths:
        try:
            payload = json.loads(lease_path.read_text(encoding="utf-8"))
            state = _activation_state(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            uncertain.append(lease_path)
            continue
        if state:
            authorized.append(lease_path)
        else:
            incomplete.append(lease_path)

    if uncertain:
        names = ", ".join(path.name for path in uncertain)
        detail = (
            "Vigil found an unreadable legacy controller-isolation journal and could "
            "not verify controller pass-through. Open HidHide, turn off Enable device "
            f"hiding, then restart Vigil. Preserved journal: {names}"
        )
        _LOGGER.critical(detail)
        return HidHideRetirementOutcome(False, detail)

    if authorized:
        if sys.platform != "win32":
            detail = "Legacy HidHide pass-through recovery requires Windows."
            _LOGGER.critical(detail)
            return HidHideRetirementOutcome(False, detail)
        cli_path = _find_hidhide_cli()
        if cli_path is None:
            detail = (
                "Vigil found an authorized legacy controller-isolation lease but the "
                "official HidHide client is unavailable. Open HidHide, turn off Enable "
                "device hiding, then restart Vigil."
            )
            _LOGGER.critical(detail)
            return HidHideRetirementOutcome(False, detail)
        try:
            state = _query_cloak_state(cli_path)
            if state:
                _set_cloak_off(cli_path)
            if _query_cloak_state(cli_path):
                raise OSError("HidHide still reports device hiding enabled")
        except (OSError, subprocess.SubprocessError) as exc:
            detail = (
                "Vigil could not verify controller pass-through while retiring its old "
                f"HidHide lease: {exc}. Open HidHide, turn off Enable device hiding, "
                "then restart Vigil."
            )
            _LOGGER.critical(detail)
            return HidHideRetirementOutcome(False, detail)

    recovered = len(authorized)
    for lease_path in (*authorized, *incomplete):
        _cleanup_lease_files(lease_path)
    cleanup_failures = _remove_vigil_metadata(user_data_root, install_root)
    detail = (
        f"Retired {recovered} legacy Vigil controller-isolation lease(s); controller "
        "pass-through is verified."
        if recovered
        else "No active legacy Vigil controller-isolation lease required recovery."
    )
    if cleanup_failures:
        detail += " Some inactive Vigil retirement metadata could not be removed."
        _LOGGER.warning("%s Paths: %s", detail, ", ".join(cleanup_failures))
    else:
        _LOGGER.info(detail)
    return HidHideRetirementOutcome(True, detail, recovered)


def _activation_state(payload: object) -> bool:
    if not isinstance(payload, dict):
        raise ValueError("lease root is not an object")
    schema = payload.get("schema")
    owner_pid = payload.get("owner_pid")
    if (
        type(schema) is not int
        or schema not in {1, 2, 3}
        or type(owner_pid) is not int
        or owner_pid <= 0
    ):
        raise ValueError("unsupported legacy lease")
    if schema in {1, 2}:
        return True
    authorized = payload.get("activation_authorized")
    if type(authorized) is not bool:
        raise ValueError("invalid activation journal")
    return authorized


def _find_hidhide_cli() -> Path | None:
    candidates: list[Path] = []
    registered = _registered_hidhide_root()
    if registered is not None:
        candidates.extend(_cli_candidates(registered))
    for variable in ("ProgramFiles", "ProgramW6432"):
        raw_root = os.environ.get(variable)
        if not raw_root:
            continue
        program_files = Path(raw_root)
        for vendor in (
            "Nefarius Software Solutions",
            "Nefarius Software Solutions e.U.",
            "Nefarius Software Solutions e.U",
        ):
            candidates.extend(_cli_candidates(program_files / vendor / "HidHide"))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _registered_hidhide_root() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Nefarius Software Solutions e.U.\HidHide",
            0,
            access,
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "Path")
    except (OSError, ImportError):
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value.strip())


def _cli_candidates(root: Path) -> tuple[Path, ...]:
    return (
        root / "HidHideCLI.exe",
        root / "x64" / "HidHideCLI.exe",
        root / "bin" / "HidHideCLI.exe",
        root / "bin" / "x64" / "HidHideCLI.exe",
    )


def _run_cli(cli_path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        (str(cli_path), *arguments),
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
        raise OSError(detail or f"HidHideCLI exited with {completed.returncode}")
    return completed.stdout.strip()


def _query_cloak_state(cli_path: Path) -> bool:
    output = _run_cli(cli_path, "--cloak-state")
    states = [line.strip().casefold() for line in output.splitlines() if line.strip()]
    if states and states[-1] == "--cloak-on":
        return True
    if states and states[-1] == "--cloak-off":
        return False
    raise OSError("HidHideCLI returned an unknown cloaking state")


def _set_cloak_off(cli_path: Path) -> None:
    _run_cli(cli_path, "--cloak-off")


def _cleanup_lease_files(lease_path: Path) -> None:
    for path in (
        lease_path,
        lease_path.with_suffix(".status.json"),
        lease_path.with_suffix(".release"),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            _LOGGER.warning("Could not remove retired Vigil lease state: %s", path)


def _remove_vigil_metadata(user_data_root: Path, install_root: Path) -> tuple[str, ...]:
    paths = (
        user_data_root / _LEASE_DIRECTORY_NAME / _OWNERSHIP_FILENAME,
        install_root / _FRESH_INSTALL_MARKER,
    )
    failures: list[str] = []
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failures.append(str(path))
    if sys.platform == "win32":
        try:
            import winreg

            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, _RECEIPT_KEY)
        except FileNotFoundError:
            pass
        except (OSError, ImportError):
            failures.append(f"HKLM\\{_RECEIPT_KEY}")
    return tuple(failures)


__all__ = [
    "HidHideRetirementOutcome",
    "retire_platform_hidhide_state",
]
