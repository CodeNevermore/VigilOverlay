"""Windows Core Audio control boundary for the first-party Audio widget."""

from __future__ import annotations

import importlib
import logging
import os
import sys
import warnings
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

_LOGGER = logging.getLogger("vigil_overlay")
_MAX_MIXER_SESSIONS = 16


class AudioControlError(RuntimeError):
    """Raised when a requested audio operation cannot be completed."""


@dataclass(frozen=True, slots=True)
class AudioDeviceInfo:
    """One active Windows render or capture endpoint."""

    device_id: str
    name: str


@dataclass(frozen=True, slots=True)
class AudioSessionInfo:
    """One process-level entry shown in Vigil's volume mixer."""

    session_id: str
    name: str
    process_path: str | None
    volume_percent: int
    muted: bool


@dataclass(frozen=True, slots=True)
class AudioSnapshot:
    """Current audio state consumed by the Audio widget."""

    available: bool
    detail: str
    output_volume_percent: int = 0
    output_muted: bool = False
    input_volume_percent: int = 0
    input_muted: bool = False
    default_output_device_id: str | None = None
    default_input_device_id: str | None = None
    output_devices: tuple[AudioDeviceInfo, ...] = ()
    input_devices: tuple[AudioDeviceInfo, ...] = ()
    sessions: tuple[AudioSessionInfo, ...] = ()


class AudioControlBackend(Protocol):
    """Backend contract used by the controller-first Audio widget."""

    @property
    def available(self) -> bool: ...

    def snapshot(self) -> AudioSnapshot: ...

    def microphone_muted(self) -> bool: ...

    def set_output_volume(self, percent: int) -> None: ...

    def set_output_muted(self, muted: bool) -> None: ...

    def set_input_volume(self, percent: int) -> None: ...

    def set_input_muted(self, muted: bool) -> None: ...

    def set_default_output_device(self, device_id: str) -> None: ...

    def set_default_input_device(self, device_id: str) -> None: ...

    def set_session_volume(self, session_id: str, percent: int) -> None: ...

    def set_session_muted(self, session_id: str, muted: bool) -> None: ...

    def close(self) -> None: ...


class UnavailableAudioControlBackend:
    """Non-Windows/failure-isolated backend."""

    def __init__(self, detail: str = "Windows audio controls are unavailable.") -> None:
        self._detail = detail

    @property
    def available(self) -> bool:
        return False

    def snapshot(self) -> AudioSnapshot:
        return AudioSnapshot(False, self._detail)

    def microphone_muted(self) -> bool:
        self._raise()

    def _raise(self) -> NoReturn:
        raise AudioControlError(self._detail)

    def set_output_volume(self, percent: int) -> None:
        del percent
        self._raise()

    def set_output_muted(self, muted: bool) -> None:
        del muted
        self._raise()

    def set_input_volume(self, percent: int) -> None:
        del percent
        self._raise()

    def set_input_muted(self, muted: bool) -> None:
        del muted
        self._raise()

    def set_default_output_device(self, device_id: str) -> None:
        del device_id
        self._raise()

    def set_default_input_device(self, device_id: str) -> None:
        del device_id
        self._raise()

    def set_session_volume(self, session_id: str, percent: int) -> None:
        del session_id, percent
        self._raise()

    def set_session_muted(self, session_id: str, muted: bool) -> None:
        del session_id, muted
        self._raise()

    def close(self) -> None:
        return


class PycawAudioControlBackend:
    """Windows Core Audio backend using the bundled Pycaw/comtypes bindings."""

    def __init__(self) -> None:
        try:
            self._pycaw = importlib.import_module("pycaw.pycaw")
            self._constants = importlib.import_module("pycaw.constants")
            self._comtypes = importlib.import_module("comtypes")
        except ImportError as exc:
            raise AudioControlError(
                "The bundled Windows audio runtime is unavailable."
            ) from exc
        try:
            self._comtypes.CoInitialize()
        except Exception as exc:
            raise AudioControlError("Windows audio COM initialization failed.") from exc
        self._closed = False

    @property
    def available(self) -> bool:
        return True

    @property
    def _audio_utilities(self) -> Any:
        return self._pycaw.AudioUtilities

    def snapshot(self) -> AudioSnapshot:
        try:
            output = self._audio_utilities.GetSpeakers()
            capture = self._audio_utilities.GetMicrophone()
            output_devices = self._active_devices(
                self._constants.EDataFlow.eRender.value
            )
            input_devices = self._active_devices(
                self._constants.EDataFlow.eCapture.value
            )
            output_volume = self._endpoint_volume(output)
            input_volume = self._endpoint_volume(capture)
            return AudioSnapshot(
                available=True,
                detail="Windows Core Audio controls are active.",
                output_volume_percent=_percent(
                    output_volume.GetMasterVolumeLevelScalar()
                ),
                output_muted=bool(output_volume.GetMute()),
                input_volume_percent=_percent(
                    input_volume.GetMasterVolumeLevelScalar()
                ),
                input_muted=bool(input_volume.GetMute()),
                default_output_device_id=self._device_id(output),
                default_input_device_id=self._device_id(capture),
                output_devices=output_devices,
                input_devices=input_devices,
                sessions=self._sessions(),
            )
        except Exception as exc:
            raise AudioControlError(
                f"Could not read Windows audio state: {exc}"
            ) from exc

    def microphone_muted(self) -> bool:
        """Return the current mute state of the default capture endpoint."""

        try:
            capture = self._audio_utilities.GetMicrophone()
            return bool(self._endpoint_volume(capture).GetMute())
        except AudioControlError:
            raise
        except Exception as exc:
            raise AudioControlError(
                f"Could not read microphone mute state: {exc}"
            ) from exc

    def set_output_volume(self, percent: int) -> None:
        self._set_endpoint_volume(self._audio_utilities.GetSpeakers(), percent)

    def set_output_muted(self, muted: bool) -> None:
        self._set_endpoint_mute(self._audio_utilities.GetSpeakers(), muted)

    def set_input_volume(self, percent: int) -> None:
        self._set_endpoint_volume(self._audio_utilities.GetMicrophone(), percent)

    def set_input_muted(self, muted: bool) -> None:
        self._set_endpoint_mute(self._audio_utilities.GetMicrophone(), muted)

    def set_default_output_device(self, device_id: str) -> None:
        self._set_default_device(device_id)

    def set_default_input_device(self, device_id: str) -> None:
        self._set_default_device(device_id)

    def set_session_volume(self, session_id: str, percent: int) -> None:
        matched = False
        for session in self._audio_utilities.GetAllSessions():
            if _session_key(session) != session_id:
                continue
            session.SimpleAudioVolume.SetMasterVolume(_scalar(percent), None)
            matched = True
        if not matched:
            raise AudioControlError("That audio session is no longer available.")

    def set_session_muted(self, session_id: str, muted: bool) -> None:
        matched = False
        for session in self._audio_utilities.GetAllSessions():
            if _session_key(session) != session_id:
                continue
            session.SimpleAudioVolume.SetMute(int(muted), None)
            matched = True
        if not matched:
            raise AudioControlError("That audio session is no longer available.")

    def _active_devices(self, data_flow: int) -> tuple[AudioDeviceInfo, ...]:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            devices = self._audio_utilities.GetAllDevices(
                data_flow=data_flow,
                device_state=self._constants.DEVICE_STATE.ACTIVE.value,
            )
        resolved = [
            AudioDeviceInfo(str(device.id), str(device.FriendlyName))
            for device in devices
        ]
        resolved.sort(key=lambda item: item.name.casefold())
        return tuple(resolved)

    def _sessions(self) -> tuple[AudioSessionInfo, ...]:
        grouped: dict[str, AudioSessionInfo] = {}
        for session in self._audio_utilities.GetAllSessions():
            try:
                key = _session_key(session)
                process = getattr(session, "Process", None)
                process_path: str | None = None
                if process is not None:
                    try:
                        name = str(process.name())
                    except Exception:
                        name = f"Process {getattr(process, 'pid', '')}".strip()
                    try:
                        process_path = str(process.exe())
                    except Exception:
                        process_path = None
                else:
                    display_name = str(
                        getattr(session, "DisplayName", "") or ""
                    ).strip()
                    name = display_name or "System Sounds"
                volume = session.SimpleAudioVolume
                current = AudioSessionInfo(
                    session_id=key,
                    name=_friendly_process_name(name),
                    process_path=process_path,
                    volume_percent=_percent(volume.GetMasterVolume()),
                    muted=bool(volume.GetMute()),
                )
                existing = grouped.get(key)
                if existing is None or (
                    existing.process_path is None and process_path is not None
                ):
                    grouped[key] = current
            except Exception:
                _LOGGER.debug(
                    "Skipping unreadable Windows audio session", exc_info=True
                )
        sessions = sorted(
            grouped.values(),
            key=lambda item: (
                item.name.casefold() != "system sounds",
                item.name.casefold(),
            ),
        )
        return tuple(sessions[:_MAX_MIXER_SESSIONS])

    def _endpoint_volume(self, device: Any) -> Any:
        """Resolve endpoint-volume control across wrapped and raw Pycaw devices."""

        wrapped_volume = getattr(device, "EndpointVolume", None)
        if wrapped_volume is not None:
            return wrapped_volume
        try:
            interface_type = self._pycaw.IAudioEndpointVolume
            activated = device.Activate(
                interface_type._iid_,
                self._comtypes.CLSCTX_ALL,
                None,
            )
            return activated.QueryInterface(interface_type)
        except Exception as exc:
            raise AudioControlError(
                f"Could not access Windows endpoint volume control: {exc}"
            ) from exc

    @staticmethod
    def _device_id(device: Any) -> str:
        """Read a stable endpoint ID from wrapped or raw IMMDevice values."""

        wrapped_id = getattr(device, "id", None)
        if wrapped_id is not None:
            return str(wrapped_id)
        try:
            return str(device.GetId())
        except Exception as exc:
            raise AudioControlError(
                f"Could not read Windows audio device ID: {exc}"
            ) from exc

    def _set_endpoint_volume(self, device: Any, percent: int) -> None:
        try:
            self._endpoint_volume(device).SetMasterVolumeLevelScalar(
                _scalar(percent), None
            )
        except AudioControlError:
            raise
        except Exception as exc:
            raise AudioControlError(f"Could not change audio volume: {exc}") from exc

    def _set_endpoint_mute(self, device: Any, muted: bool) -> None:
        try:
            self._endpoint_volume(device).SetMute(int(muted), None)
        except AudioControlError:
            raise
        except Exception as exc:
            raise AudioControlError(
                f"Could not change audio mute state: {exc}"
            ) from exc

    def _set_default_device(self, device_id: str) -> None:
        try:
            self._audio_utilities.SetDefaultDevice(device_id)
        except Exception as exc:
            raise AudioControlError(
                f"Could not change the default audio device: {exc}"
            ) from exc

    def close(self) -> None:
        """Release the COM apartment on the thread that initialized it."""

        if self._closed:
            return
        self._closed = True
        try:
            self._comtypes.CoUninitialize()
        except Exception:
            _LOGGER.debug("Windows audio COM uninitialization failed", exc_info=True)


def create_platform_audio_control_backend() -> AudioControlBackend:
    """Create the Windows audio backend without making it a startup dependency."""

    if sys.platform != "win32" or os.name != "nt":
        return UnavailableAudioControlBackend(
            "Audio controls are available on Windows only."
        )
    try:
        return PycawAudioControlBackend()
    except AudioControlError as exc:
        _LOGGER.warning("Windows audio backend unavailable: %s", exc)
        return UnavailableAudioControlBackend(str(exc))


def _session_key(session: Any) -> str:
    process = getattr(session, "Process", None)
    if process is not None:
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            return f"process:{pid}"
    process_id = getattr(session, "ProcessId", None)
    if isinstance(process_id, int) and process_id > 0:
        return f"process:{process_id}"
    identifier = str(getattr(session, "Identifier", "") or "").strip()
    if identifier:
        return f"session:{identifier}"
    instance = str(getattr(session, "InstanceIdentifier", "") or "").strip()
    if instance:
        return f"session:{instance}"
    return "session:system-sounds"


def _friendly_process_name(name: str) -> str:
    cleaned = name.strip() or "Audio Session"
    normalized = cleaned.casefold().replace("/", "\\")
    if "audiosrv.dll" in normalized:
        return "System Sounds"
    if cleaned.casefold().endswith(".exe"):
        cleaned = cleaned[:-4]
    return cleaned.replace("_", " ")


def _percent(value: float) -> int:
    return min(max(round(float(value) * 100.0), 0), 100)


def _scalar(percent: int) -> float:
    return min(max(int(percent), 0), 100) / 100.0
