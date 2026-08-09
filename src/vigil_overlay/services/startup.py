"""Per-user Windows startup registration for the installed Vigil executable."""

from __future__ import annotations

import importlib
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from vigil_overlay.core.runtime import packaged_executable_path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "VigilOverlay"
_TASK_NAME = r"\VigilOverlay"
_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_TASK_TIMEOUT_SECONDS = 15.0


class StartupBackend(Protocol):
    """Minimal persistence boundary for one per-user startup command."""

    def read_command(self) -> str | None: ...

    def write_command(self, command: str) -> None: ...

    def remove_command(self) -> None: ...


def _load_winreg() -> Any:
    return importlib.import_module("winreg")


class WindowsRegistryStartupBackend:
    """Legacy Run-key owner retained only for exact migration cleanup."""

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
        if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} or not isinstance(value, str):
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


class WindowsTaskSchedulerStartupBackend:
    """Store an elevated per-user logon task through the Windows Task Scheduler."""

    def read_command(self) -> str | None:
        completed = _run_schtasks("/Query", "/TN", _TASK_NAME, "/XML")
        if completed.returncode != 0:
            return None
        registration = completed.stdout.strip()
        if not registration:
            raise OSError("Task Scheduler returned an empty Vigil startup task")
        try:
            ET.fromstring(registration)
        except ET.ParseError as exc:
            raise OSError("Task Scheduler returned invalid Vigil task XML") from exc
        return registration

    def write_command(self, command: str) -> None:
        try:
            ET.fromstring(command)
        except ET.ParseError as exc:
            raise OSError("Vigil startup task definition is invalid") from exc

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-16",
                suffix=".xml",
                prefix="VigilOverlay-startup-",
                delete=False,
            ) as handle:
                handle.write(command)
                temporary_path = Path(handle.name)
            completed = _run_schtasks(
                "/Create",
                "/TN",
                _TASK_NAME,
                "/XML",
                str(temporary_path),
                "/F",
            )
            if completed.returncode != 0:
                raise OSError(_schtasks_failure("create", completed))
            WindowsRegistryStartupBackend().remove_command()
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def remove_command(self) -> None:
        completed = _run_schtasks("/Delete", "/TN", _TASK_NAME, "/F")
        if completed.returncode not in {0, 1}:
            raise OSError(_schtasks_failure("remove", completed))
        WindowsRegistryStartupBackend().remove_command()


def _run_schtasks(*arguments: str) -> subprocess.CompletedProcess[str]:
    executable = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "schtasks.exe"
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return subprocess.run(
            [str(executable), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TASK_TIMEOUT_SECONDS,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"Could not run Windows Task Scheduler: {exc}") from exc


def _schtasks_failure(action: str, completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout).strip()
    suffix = f": {detail}" if detail else ""
    return f"Could not {action} the Vigil startup task (exit {completed.returncode}){suffix}"


def _startup_task_xml(executable_path: Path) -> str:
    """Build the canonical highest-privilege interactive logon task definition."""

    ET.register_namespace("", _TASK_NAMESPACE)

    def tag(name: str) -> str:
        return f"{{{_TASK_NAMESPACE}}}{name}"

    root = ET.Element(tag("Task"), {"version": "1.4"})
    registration = ET.SubElement(root, tag("RegistrationInfo"))
    ET.SubElement(registration, tag("Author")).text = "Vigil Overlay"
    ET.SubElement(
        registration, tag("Description")
    ).text = "Launch Vigil Overlay elevated when the current user signs in."

    triggers = ET.SubElement(root, tag("Triggers"))
    logon = ET.SubElement(triggers, tag("LogonTrigger"))
    ET.SubElement(logon, tag("Enabled")).text = "true"

    principals = ET.SubElement(root, tag("Principals"))
    principal = ET.SubElement(principals, tag("Principal"), {"id": "Author"})
    identity_parts = tuple(
        part
        for part in (
            os.environ.get("USERDOMAIN", "").strip(),
            os.environ.get("USERNAME", "").strip(),
        )
        if part
    )
    if identity_parts:
        ET.SubElement(principal, tag("UserId")).text = "\\".join(identity_parts)
    ET.SubElement(principal, tag("LogonType")).text = "InteractiveToken"
    ET.SubElement(principal, tag("RunLevel")).text = "HighestAvailable"

    settings = ET.SubElement(root, tag("Settings"))
    ET.SubElement(settings, tag("MultipleInstancesPolicy")).text = "IgnoreNew"
    ET.SubElement(settings, tag("DisallowStartIfOnBatteries")).text = "false"
    ET.SubElement(settings, tag("StopIfGoingOnBatteries")).text = "false"
    ET.SubElement(settings, tag("AllowHardTerminate")).text = "true"
    ET.SubElement(settings, tag("StartWhenAvailable")).text = "true"
    ET.SubElement(settings, tag("RunOnlyIfNetworkAvailable")).text = "false"
    ET.SubElement(settings, tag("AllowStartOnDemand")).text = "true"
    ET.SubElement(settings, tag("Enabled")).text = "true"
    ET.SubElement(settings, tag("Hidden")).text = "false"
    ET.SubElement(settings, tag("RunOnlyIfIdle")).text = "false"
    ET.SubElement(settings, tag("WakeToRun")).text = "false"
    ET.SubElement(settings, tag("ExecutionTimeLimit")).text = "PT0S"
    ET.SubElement(settings, tag("Priority")).text = "7"

    actions = ET.SubElement(root, tag("Actions"), {"Context": "Author"})
    execute = ET.SubElement(actions, tag("Exec"))
    ET.SubElement(execute, tag("Command")).text = str(executable_path.resolve())
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-16"?>\n{body}'


def _task_registration_matches(registration: str, executable_path: Path) -> bool:
    try:
        root = ET.fromstring(registration)
    except ET.ParseError:
        return False
    namespace = {"task": _TASK_NAMESPACE}
    command = root.findtext("task:Actions/task:Exec/task:Command", namespaces=namespace)
    arguments = root.findtext("task:Actions/task:Exec/task:Arguments", namespaces=namespace)
    run_level = root.findtext("task:Principals/task:Principal/task:RunLevel", namespaces=namespace)
    logon_type = root.findtext(
        "task:Principals/task:Principal/task:LogonType", namespaces=namespace
    )
    logon_trigger = root.find("task:Triggers/task:LogonTrigger", namespace)
    if command is None or logon_trigger is None:
        return False
    enabled = logon_trigger.findtext("task:Enabled", default="true", namespaces=namespace)
    expected = os.path.normcase(os.path.abspath(executable_path.resolve()))
    observed = os.path.normcase(os.path.abspath(command.strip().strip('"')))
    return bool(
        observed == expected
        and not (arguments or "").strip()
        and run_level == "HighestAvailable"
        and logon_type == "InteractiveToken"
        and enabled.casefold() != "false"
    )


@dataclass(frozen=True, slots=True)
class StartupRegistrationState:
    """Observed state for the current installed executable."""

    supported: bool
    enabled: bool
    command: str | None
    detail: str


class StartupRegistrationService:
    """Own elevated logon-task reconciliation and exact rollback support."""

    def __init__(
        self,
        backend: StartupBackend | None,
        executable_path: Path | None,
    ) -> None:
        self._backend = backend
        self._executable_path = executable_path.resolve() if executable_path is not None else None

    @property
    def supported(self) -> bool:
        return self._backend is not None and self._executable_path is not None

    @property
    def desired_command(self) -> str | None:
        if self._executable_path is None:
            return None
        return _startup_task_xml(self._executable_path)

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
        enabled = bool(
            command is not None
            and self._executable_path is not None
            and _task_registration_matches(command, self._executable_path)
        )
        detail = "Registered as an elevated logon task" if enabled else "Not registered"
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
        """Make Task Scheduler match the preference, repairing stale tasks."""

        if not self.supported:
            return self.state()
        current = self.read_command()
        desired = self.desired_command
        current_matches = bool(
            current is not None
            and self._executable_path is not None
            and _task_registration_matches(current, self._executable_path)
        )
        if enabled and not current_matches:
            assert self._backend is not None
            assert desired is not None
            self._backend.write_command(desired)
        elif not enabled:
            assert self._backend is not None
            self._backend.remove_command()
        return self.state()

    def restore_command(self, command: str | None) -> None:
        """Restore an exact pre-change task XML snapshot after a failed transaction."""

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
    return StartupRegistrationService(WindowsTaskSchedulerStartupBackend(), executable)
