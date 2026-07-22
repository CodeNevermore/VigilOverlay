"""Windows display-mode and projection configuration boundary.

The UI consumes this module through a small backend protocol so Windows-native
mode changes remain isolated and replaceable without teaching the Display widget
about ctypes or Win32 constants.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

_LOGGER = logging.getLogger("vigil_overlay")


class DisplayConfigurationError(RuntimeError):
    """Raised when Windows rejects a requested display configuration change."""


class ProjectionMode(StrEnum):
    """Windows display topologies exposed by the Display widget."""

    PC_SCREEN_ONLY = "pc_screen_only"
    DUPLICATE = "duplicate"
    EXTEND = "extend"
    SECOND_SCREEN_ONLY = "second_screen_only"

    @property
    def label(self) -> str:
        return {
            ProjectionMode.PC_SCREEN_ONLY: "PC screen only",
            ProjectionMode.DUPLICATE: "Duplicate",
            ProjectionMode.EXTEND: "Extend",
            ProjectionMode.SECOND_SCREEN_ONLY: "Second screen only",
        }[self]


@dataclass(frozen=True, order=True)
class DisplayMode:
    """One enumerated resolution and refresh-rate combination."""

    width: int
    height: int
    refresh_hz: float
    native_devmode: bytes | None = field(default=None, compare=False, repr=False)

    @property
    def resolution_label(self) -> str:
        return f"{self.width} x {self.height}"

    @property
    def refresh_label(self) -> str:
        if abs(self.refresh_hz - round(self.refresh_hz)) < 0.05:
            return f"{round(self.refresh_hz)} Hz"
        return f"{self.refresh_hz:.2f} Hz"


@dataclass(frozen=True)
class DisplayCapabilities:
    """Current and selectable mode/topology values for one display."""

    current_mode: DisplayMode | None
    modes: tuple[DisplayMode, ...]
    current_projection: ProjectionMode | None
    projections: tuple[ProjectionMode, ...]


@runtime_checkable
class DisplayConfigurationBackend(Protocol):
    """Host-owned backend used by the controller-first Display widget."""

    @property
    def available(self) -> bool: ...

    def resolve_display_name(
        self,
        window_handle: int | None,
        fallback_name: str | None,
    ) -> str | None: ...

    def capabilities(self, display_name: str | None) -> DisplayCapabilities: ...

    def apply_mode(self, display_name: str | None, mode: DisplayMode) -> None: ...

    def commit_mode(self, display_name: str | None, mode: DisplayMode) -> None: ...

    def apply_projection(self, projection: ProjectionMode) -> None: ...

    def commit_projection(self, projection: ProjectionMode) -> None: ...


class UnavailableDisplayConfigurationBackend:
    """Non-Windows/failure fallback that keeps the Display surface readable."""

    @property
    def available(self) -> bool:
        return False

    def resolve_display_name(
        self,
        window_handle: int | None,
        fallback_name: str | None,
    ) -> str | None:
        del window_handle
        return fallback_name

    def capabilities(self, display_name: str | None) -> DisplayCapabilities:
        del display_name
        return DisplayCapabilities(None, (), None, ())

    def apply_mode(self, display_name: str | None, mode: DisplayMode) -> None:
        del display_name, mode
        raise DisplayConfigurationError("Windows display configuration is unavailable")

    def commit_mode(self, display_name: str | None, mode: DisplayMode) -> None:
        del display_name, mode
        raise DisplayConfigurationError("Windows display configuration is unavailable")

    def apply_projection(self, projection: ProjectionMode) -> None:
        del projection
        raise DisplayConfigurationError("Windows display configuration is unavailable")

    def commit_projection(self, projection: ProjectionMode) -> None:
        del projection
        raise DisplayConfigurationError("Windows display configuration is unavailable")


_ENUM_CURRENT_SETTINGS = -1
_ENUM_REGISTRY_SETTINGS = -2
_DISP_CHANGE_SUCCESSFUL = 0
_CDS_UPDATEREGISTRY = 0x00000001
_CDS_TEST = 0x00000002

_DM_PELSWIDTH = 0x00080000
_DM_PELSHEIGHT = 0x00100000
_DM_DISPLAYFREQUENCY = 0x00400000

_QDC_ONLY_ACTIVE_PATHS = 0x00000002
_QDC_DATABASE_CURRENT = 0x00000004
_SDC_TOPOLOGY_INTERNAL = 0x00000001
_SDC_TOPOLOGY_CLONE = 0x00000002
_SDC_TOPOLOGY_EXTEND = 0x00000004
_SDC_TOPOLOGY_EXTERNAL = 0x00000008
_SDC_APPLY = 0x00000080
_SDC_VALIDATE = 0x00000040
_SDC_ALLOW_CHANGES = 0x00000400
_MONITOR_DEFAULTTONEAREST = 0x00000002
_DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
_DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9
_DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_STATE = 10

_DISP_CHANGE_NAMES = {
    0: "DISP_CHANGE_SUCCESSFUL",
    1: "DISP_CHANGE_RESTART",
    -1: "DISP_CHANGE_FAILED",
    -2: "DISP_CHANGE_BADMODE",
    -3: "DISP_CHANGE_NOTUPDATED",
    -4: "DISP_CHANGE_BADFLAGS",
    -5: "DISP_CHANGE_BADPARAM",
    -6: "DISP_CHANGE_BADDUALVIEW",
}


class _POINTL(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPosition", _POINTL),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", wintypes.SHORT),
        ("dmDuplex", wintypes.SHORT),
        ("dmYResolution", wintypes.SHORT),
        ("dmTTOption", wintypes.SHORT),
        ("dmCollate", wintypes.SHORT),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.UINT),
        ("size", wintypes.UINT),
        ("adapterId", _LUID),
        ("id", wintypes.UINT),
    ]


class _DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", _DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("viewGdiDeviceName", wintypes.WCHAR * 32),
    ]


class _DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO(ctypes.Structure):
    _fields_ = [
        ("header", _DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("value", wintypes.UINT),
        ("colorEncoding", wintypes.UINT),
        ("bitsPerColorChannel", wintypes.UINT),
    ]


class _DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE(ctypes.Structure):
    _fields_ = [
        ("header", _DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("value", wintypes.UINT),
    ]


@dataclass(frozen=True, slots=True)
class _AdvancedColorState:
    supported: bool
    enabled: bool


class _DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]


class _DISPLAYCONFIG_2DREGION(ctypes.Structure):
    _fields_ = [("cx", wintypes.UINT), ("cy", wintypes.UINT)]


class _DISPLAYCONFIG_VIDEO_SIGNAL_INFO(ctypes.Structure):
    _fields_ = [
        ("pixelRate", ctypes.c_uint64),
        ("hSyncFreq", _DISPLAYCONFIG_RATIONAL),
        ("vSyncFreq", _DISPLAYCONFIG_RATIONAL),
        ("activeSize", _DISPLAYCONFIG_2DREGION),
        ("totalSize", _DISPLAYCONFIG_2DREGION),
        ("videoStandard", wintypes.UINT),
        ("scanLineOrdering", wintypes.UINT),
    ]


class _DISPLAYCONFIG_TARGET_MODE(ctypes.Structure):
    _fields_ = [("targetVideoSignalInfo", _DISPLAYCONFIG_VIDEO_SIGNAL_INFO)]


class _DISPLAYCONFIG_SOURCE_MODE(ctypes.Structure):
    _fields_ = [
        ("width", wintypes.UINT),
        ("height", wintypes.UINT),
        ("pixelFormat", wintypes.UINT),
        ("position", _POINTL),
    ]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _DISPLAYCONFIG_DESKTOP_IMAGE_INFO(ctypes.Structure):
    _fields_ = [
        ("PathSourceSize", _DISPLAYCONFIG_2DREGION),
        ("DesktopImageRegion", _RECT),
        ("DesktopImageClip", _RECT),
    ]


class _DISPLAYCONFIG_MODE_UNION(ctypes.Union):
    _fields_ = [  # noqa: RUF012 - ctypes requires this class-level declaration.
        ("targetMode", _DISPLAYCONFIG_TARGET_MODE),
        ("sourceMode", _DISPLAYCONFIG_SOURCE_MODE),
        ("desktopImageInfo", _DISPLAYCONFIG_DESKTOP_IMAGE_INFO),
    ]


class _DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    _anonymous_ = ("mode",)
    _fields_ = [
        ("infoType", wintypes.UINT),
        ("id", wintypes.UINT),
        ("adapterId", _LUID),
        ("mode", _DISPLAYCONFIG_MODE_UNION),
    ]


class _DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", _LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("statusFlags", wintypes.UINT),
    ]


class _DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", _LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("outputTechnology", wintypes.UINT),
        ("rotation", wintypes.UINT),
        ("scaling", wintypes.UINT),
        ("refreshRate", _DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", wintypes.UINT),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", wintypes.UINT),
    ]


class _DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", _DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", _DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", wintypes.UINT),
    ]


_TOPOLOGY_TO_FLAG = {
    ProjectionMode.PC_SCREEN_ONLY: _SDC_TOPOLOGY_INTERNAL,
    ProjectionMode.DUPLICATE: _SDC_TOPOLOGY_CLONE,
    ProjectionMode.EXTEND: _SDC_TOPOLOGY_EXTEND,
    ProjectionMode.SECOND_SCREEN_ONLY: _SDC_TOPOLOGY_EXTERNAL,
}
_TOPOLOGY_ID_TO_MODE = {
    _SDC_TOPOLOGY_INTERNAL: ProjectionMode.PC_SCREEN_ONLY,
    _SDC_TOPOLOGY_CLONE: ProjectionMode.DUPLICATE,
    _SDC_TOPOLOGY_EXTEND: ProjectionMode.EXTEND,
    _SDC_TOPOLOGY_EXTERNAL: ProjectionMode.SECOND_SCREEN_ONLY,
}


class WindowsDisplayConfigurationBackend:
    """Native Windows display configuration using documented User32 APIs."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows display configuration requires Windows")
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise OSError("Windows DLL loader is unavailable")
        self._user32: Any = loader("user32", use_last_error=True)
        self._configure_signatures()

    @property
    def available(self) -> bool:
        return True

    def resolve_display_name(
        self,
        window_handle: int | None,
        fallback_name: str | None,
    ) -> str | None:
        """Resolve the exact Win32 display device containing Vigil's top-level HWND."""

        if window_handle:
            monitor = self._user32.MonitorFromWindow(
                wintypes.HWND(window_handle),
                _MONITOR_DEFAULTTONEAREST,
            )
            if monitor:
                info = _MONITORINFOEXW()
                info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
                if self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    device_name = str(info.szDevice).strip()
                    if device_name:
                        return self._normalize_display_name(device_name)
        return self._normalize_display_name(fallback_name)

    def capabilities(self, display_name: str | None) -> DisplayCapabilities:
        normalized = self._normalize_display_name(display_name)
        current = self._current_mode(normalized)
        modes = self._enumerate_modes(normalized)
        current_projection = self._current_projection()
        projections = tuple(
            projection
            for projection in ProjectionMode
            if self._projection_supported(projection)
        )
        return DisplayCapabilities(current, modes, current_projection, projections)

    def apply_mode(self, display_name: str | None, mode: DisplayMode) -> None:
        self._change_mode(display_name, mode, persist=False)

    def commit_mode(self, display_name: str | None, mode: DisplayMode) -> None:
        self._change_mode(display_name, mode, persist=True)

    def apply_projection(self, projection: ProjectionMode) -> None:
        self._set_projection(projection, persist=False)

    def commit_projection(self, projection: ProjectionMode) -> None:
        # Standard topology flags select a Windows-managed topology database entry.
        # Keeping the change means leaving that validated topology active; unlike a
        # supplied path array, SDC_SAVE_TO_DATABASE is not a legal topology-flag mix.
        self._set_projection(projection, persist=False)

    def _configure_signatures(self) -> None:
        self._user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self._user32.MonitorFromWindow.restype = wintypes.HANDLE
        self._user32.GetMonitorInfoW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_MONITORINFOEXW),
        ]
        self._user32.GetMonitorInfoW.restype = wintypes.BOOL
        self._user32.EnumDisplaySettingsExW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(_DEVMODEW),
            wintypes.DWORD,
        ]
        self._user32.EnumDisplaySettingsExW.restype = wintypes.BOOL
        self._user32.ChangeDisplaySettingsExW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(_DEVMODEW),
            wintypes.HWND,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        self._user32.ChangeDisplaySettingsExW.restype = wintypes.LONG
        self._user32.GetDisplayConfigBufferSizes.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ]
        self._user32.GetDisplayConfigBufferSizes.restype = wintypes.LONG
        self._user32.QueryDisplayConfig.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(_DISPLAYCONFIG_PATH_INFO),
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(_DISPLAYCONFIG_MODE_INFO),
            ctypes.POINTER(wintypes.UINT),
        ]
        self._user32.QueryDisplayConfig.restype = wintypes.LONG
        self._user32.SetDisplayConfig.argtypes = [
            wintypes.UINT,
            ctypes.c_void_p,
            wintypes.UINT,
            ctypes.c_void_p,
            wintypes.UINT,
        ]
        self._user32.SetDisplayConfig.restype = wintypes.LONG
        self._user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.c_void_p]
        self._user32.DisplayConfigGetDeviceInfo.restype = wintypes.LONG
        self._user32.DisplayConfigSetDeviceInfo.argtypes = [ctypes.c_void_p]
        self._user32.DisplayConfigSetDeviceInfo.restype = wintypes.LONG

    @staticmethod
    def _normalize_display_name(display_name: str | None) -> str | None:
        if not display_name:
            return None
        if display_name.startswith("\\\\.\\"):
            return display_name
        if display_name.upper().startswith("DISPLAY"):
            return f"\\\\.\\{display_name}"
        return display_name

    @staticmethod
    def _new_devmode() -> _DEVMODEW:
        mode = _DEVMODEW()
        mode.dmSize = ctypes.sizeof(_DEVMODEW)
        return mode

    @staticmethod
    def _snapshot_devmode(mode: _DEVMODEW) -> bytes:
        return ctypes.string_at(ctypes.byref(mode), ctypes.sizeof(_DEVMODEW))

    @staticmethod
    def _restore_devmode(snapshot: bytes) -> _DEVMODEW:
        if len(snapshot) != ctypes.sizeof(_DEVMODEW):
            raise DisplayConfigurationError("Stored Windows display mode is invalid")
        return _DEVMODEW.from_buffer_copy(snapshot)

    @classmethod
    def _display_mode_from_native(cls, mode: _DEVMODEW) -> DisplayMode:
        return DisplayMode(
            width=int(mode.dmPelsWidth),
            height=int(mode.dmPelsHeight),
            refresh_hz=float(mode.dmDisplayFrequency),
            native_devmode=cls._snapshot_devmode(mode),
        )

    def _query_native_mode(
        self,
        display_name: str | None,
        mode_number: int,
    ) -> _DEVMODEW | None:
        mode = self._new_devmode()
        if not self._user32.EnumDisplaySettingsExW(
            display_name, mode_number, ctypes.byref(mode), 0
        ):
            return None
        return mode

    def _current_mode(self, display_name: str | None) -> DisplayMode | None:
        mode = self._query_native_mode(display_name, _ENUM_CURRENT_SETTINGS)
        return self._display_mode_from_native(mode) if mode is not None else None

    def _enumerate_modes(self, display_name: str | None) -> tuple[DisplayMode, ...]:
        modes: set[DisplayMode] = set()
        index = 0
        while True:
            mode = self._query_native_mode(display_name, index)
            if mode is None:
                break
            index += 1
            if (
                mode.dmPelsWidth <= 0
                or mode.dmPelsHeight <= 0
                or mode.dmDisplayFrequency <= 0
                or mode.dmBitsPerPel < 32
            ):
                continue
            modes.add(self._display_mode_from_native(mode))
        return tuple(
            sorted(
                modes, key=lambda value: (value.width, value.height, value.refresh_hz)
            )
        )

    def _native_for_mode(
        self,
        display_name: str | None,
        mode: DisplayMode,
    ) -> _DEVMODEW:
        if mode.native_devmode is not None:
            return self._restore_devmode(mode.native_devmode)

        # Compatibility path for programmatically constructed DisplayMode values.
        # Resolve them back to a real mode enumerated by Windows rather than
        # synthesizing a mostly empty DEVMODE structure.
        index = 0
        while True:
            native = self._query_native_mode(display_name, index)
            if native is None:
                break
            index += 1
            candidate = self._display_mode_from_native(native)
            if (
                candidate.width == mode.width
                and candidate.height == mode.height
                and abs(candidate.refresh_hz - mode.refresh_hz) < 0.05
            ):
                return native
        raise DisplayConfigurationError(
            f"Windows no longer reports {mode.resolution_label} at {mode.refresh_label} "
            "as an available native display mode"
        )

    @staticmethod
    def _mode_matches(native: _DEVMODEW | None, requested: DisplayMode) -> bool:
        if native is None:
            return False
        return (
            int(native.dmPelsWidth) == requested.width
            and int(native.dmPelsHeight) == requested.height
            and abs(float(native.dmDisplayFrequency) - requested.refresh_hz) < 0.05
        )

    @staticmethod
    def _result_name(result: int) -> str:
        return _DISP_CHANGE_NAMES.get(result, f"DISP_CHANGE_UNKNOWN({result})")

    def _apply_native_mode(
        self,
        display_name: str | None,
        native: _DEVMODEW,
        *,
        persist: bool,
    ) -> int:
        flags = _CDS_UPDATEREGISTRY if persist else 0
        return int(
            self._user32.ChangeDisplaySettingsExW(
                display_name,
                ctypes.byref(native),
                None,
                flags,
                None,
            )
        )

    def _rollback_native_mode(
        self,
        display_name: str | None,
        snapshot: _DEVMODEW | None,
        *,
        persist: bool,
    ) -> int | None:
        if snapshot is None:
            return None
        result = self._apply_native_mode(display_name, snapshot, persist=persist)
        if result != _DISP_CHANGE_SUCCESSFUL:
            _LOGGER.error(
                "Display mode rollback failed display=%s result=%s",
                display_name,
                self._result_name(result),
            )
        return result

    @staticmethod
    def _copy_luid(source: _LUID) -> _LUID:
        copied = _LUID()
        copied.LowPart = source.LowPart
        copied.HighPart = source.HighPart
        return copied

    def _active_display_target(
        self,
        display_name: str | None,
    ) -> tuple[_LUID, int] | None:
        normalized = self._normalize_display_name(display_name)
        if normalized is None:
            return None
        path_count = wintypes.UINT(0)
        mode_count = wintypes.UINT(0)
        result = int(
            self._user32.GetDisplayConfigBufferSizes(
                _QDC_ONLY_ACTIVE_PATHS,
                ctypes.byref(path_count),
                ctypes.byref(mode_count),
            )
        )
        if result != 0 or path_count.value == 0:
            return None
        paths = (_DISPLAYCONFIG_PATH_INFO * path_count.value)()
        modes = (_DISPLAYCONFIG_MODE_INFO * max(mode_count.value, 1))()
        result = int(
            self._user32.QueryDisplayConfig(
                _QDC_ONLY_ACTIVE_PATHS,
                ctypes.byref(path_count),
                paths,
                ctypes.byref(mode_count),
                modes,
                None,
            )
        )
        if result != 0:
            return None

        for index in range(path_count.value):
            path = paths[index]
            source_name = _DISPLAYCONFIG_SOURCE_DEVICE_NAME()
            source_name.header.type = _DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
            source_name.header.size = ctypes.sizeof(_DISPLAYCONFIG_SOURCE_DEVICE_NAME)
            source_name.header.adapterId = self._copy_luid(path.sourceInfo.adapterId)
            source_name.header.id = path.sourceInfo.id
            info_result = int(
                self._user32.DisplayConfigGetDeviceInfo(ctypes.byref(source_name))
            )
            if info_result != 0:
                continue
            observed = self._normalize_display_name(
                str(source_name.viewGdiDeviceName).strip()
            )
            if observed != normalized:
                continue
            return self._copy_luid(path.targetInfo.adapterId), int(path.targetInfo.id)
        return None

    def _advanced_color_state(
        self,
        display_name: str | None,
    ) -> _AdvancedColorState | None:
        target = self._active_display_target(display_name)
        if target is None:
            return None
        adapter_id, target_id = target
        info = _DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO()
        info.header.type = _DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO
        info.header.size = ctypes.sizeof(_DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO)
        info.header.adapterId = adapter_id
        info.header.id = target_id
        result = int(self._user32.DisplayConfigGetDeviceInfo(ctypes.byref(info)))
        if result != 0:
            return None
        return _AdvancedColorState(
            supported=bool(info.value & 0x1),
            enabled=bool(info.value & 0x2),
        )

    def _restore_advanced_color_state(
        self,
        display_name: str | None,
        expected: _AdvancedColorState | None,
    ) -> None:
        if expected is None or not expected.supported:
            return
        current = self._advanced_color_state(display_name)
        if current is None:
            raise DisplayConfigurationError(
                "Windows stopped reporting Advanced Color state after the display change"
            )
        if current.enabled == expected.enabled:
            return
        target = self._active_display_target(display_name)
        if target is None:
            raise DisplayConfigurationError(
                "Windows could not resolve the display target needed to restore Advanced Color"
            )
        adapter_id, target_id = target
        state = _DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE()
        state.header.type = _DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_STATE
        state.header.size = ctypes.sizeof(_DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE)
        state.header.adapterId = adapter_id
        state.header.id = target_id
        state.value = 1 if expected.enabled else 0
        result = int(self._user32.DisplayConfigSetDeviceInfo(ctypes.byref(state)))
        if result != 0:
            raise DisplayConfigurationError(
                f"Windows changed Advanced Color during the display operation and could not "
                f"restore it (result {result})"
            )
        verified = self._advanced_color_state(display_name)
        if verified is None or verified.enabled != expected.enabled:
            raise DisplayConfigurationError(
                "Windows changed Advanced Color during the display operation and the restore "
                "could not be verified"
            )

    def _change_mode(
        self,
        display_name: str | None,
        mode: DisplayMode,
        *,
        persist: bool,
    ) -> None:
        normalized = self._normalize_display_name(display_name)
        target_native = self._native_for_mode(normalized, mode)
        current_native = self._query_native_mode(normalized, _ENUM_CURRENT_SETTINGS)
        registry_native = (
            self._query_native_mode(normalized, _ENUM_REGISTRY_SETTINGS)
            if persist
            else None
        )
        rollback_native = (
            registry_native
            if persist and registry_native is not None
            else current_native
        )
        advanced_color_before = self._advanced_color_state(normalized)

        _LOGGER.debug(
            "Display mode transaction start display=%s requested=%sx%s@%s persist=%s "
            "advanced_color=%s",
            normalized,
            mode.width,
            mode.height,
            mode.refresh_hz,
            persist,
            advanced_color_before,
        )

        validation_native = self._restore_devmode(self._snapshot_devmode(target_native))
        validation_result = int(
            self._user32.ChangeDisplaySettingsExW(
                normalized,
                ctypes.byref(validation_native),
                None,
                _CDS_TEST,
                None,
            )
        )
        if validation_result != _DISP_CHANGE_SUCCESSFUL:
            raise DisplayConfigurationError(
                f"Windows rejected {mode.resolution_label} at {mode.refresh_label} during "
                f"validation ({self._result_name(validation_result)})"
            )

        apply_native = self._restore_devmode(self._snapshot_devmode(target_native))
        result = self._apply_native_mode(normalized, apply_native, persist=persist)
        if result != _DISP_CHANGE_SUCCESSFUL:
            rollback_result = self._rollback_native_mode(
                normalized,
                rollback_native,
                persist=persist,
            )
            try:
                self._restore_advanced_color_state(normalized, advanced_color_before)
            except DisplayConfigurationError as color_exc:
                _LOGGER.error(
                    "Advanced Color rollback after display failure failed: %s",
                    color_exc,
                )
            rollback_detail = (
                self._result_name(rollback_result)
                if rollback_result is not None
                else "unavailable"
            )
            raise DisplayConfigurationError(
                f"Windows could not apply {mode.resolution_label} at {mode.refresh_label} "
                f"({self._result_name(result)}); rollback={rollback_detail}"
            )

        observed_native = self._query_native_mode(normalized, _ENUM_CURRENT_SETTINGS)
        if not self._mode_matches(observed_native, mode):
            rollback_result = self._rollback_native_mode(
                normalized,
                rollback_native,
                persist=persist,
            )
            try:
                self._restore_advanced_color_state(normalized, advanced_color_before)
            except DisplayConfigurationError as color_exc:
                _LOGGER.error(
                    "Advanced Color rollback after mode mismatch failed: %s", color_exc
                )
            observed = (
                self._display_mode_from_native(observed_native)
                if observed_native is not None
                else None
            )
            rollback_detail = (
                self._result_name(rollback_result)
                if rollback_result is not None
                else "unavailable"
            )
            raise DisplayConfigurationError(
                f"Windows reported success but the requested display mode was not active "
                f"afterward (requested {mode.resolution_label} at {mode.refresh_label}; "
                f"observed {observed}; rollback={rollback_detail})"
            )

        try:
            self._restore_advanced_color_state(normalized, advanced_color_before)
        except DisplayConfigurationError as exc:
            rollback_result = self._rollback_native_mode(
                normalized,
                rollback_native,
                persist=persist,
            )
            try:
                self._restore_advanced_color_state(normalized, advanced_color_before)
            except DisplayConfigurationError as rollback_color_exc:
                _LOGGER.error(
                    "Advanced Color rollback after preservation failure failed: %s",
                    rollback_color_exc,
                )
            rollback_detail = (
                self._result_name(rollback_result)
                if rollback_result is not None
                else "rollback unavailable"
            )
            raise DisplayConfigurationError(
                f"{exc}; display mode was rolled back ({rollback_detail})"
            ) from exc

        _LOGGER.debug(
            "Display mode transaction verified display=%s requested=%sx%s@%s persist=%s",
            normalized,
            mode.width,
            mode.height,
            mode.refresh_hz,
            persist,
        )

    def _current_projection(self) -> ProjectionMode | None:
        path_count = wintypes.UINT(0)
        mode_count = wintypes.UINT(0)
        result = int(
            self._user32.GetDisplayConfigBufferSizes(
                _QDC_DATABASE_CURRENT,
                ctypes.byref(path_count),
                ctypes.byref(mode_count),
            )
        )
        if result != 0 or path_count.value == 0:
            return None
        paths = (_DISPLAYCONFIG_PATH_INFO * path_count.value)()
        modes = (_DISPLAYCONFIG_MODE_INFO * max(mode_count.value, 1))()
        topology_id = wintypes.UINT(0)
        result = int(
            self._user32.QueryDisplayConfig(
                _QDC_DATABASE_CURRENT,
                ctypes.byref(path_count),
                paths,
                ctypes.byref(mode_count),
                modes,
                ctypes.byref(topology_id),
            )
        )
        if result != 0:
            return None
        return _TOPOLOGY_ID_TO_MODE.get(int(topology_id.value))

    def _projection_supported(self, projection: ProjectionMode) -> bool:
        flags = _SDC_VALIDATE | _TOPOLOGY_TO_FLAG[projection] | _SDC_ALLOW_CHANGES
        return int(self._user32.SetDisplayConfig(0, None, 0, None, flags)) == 0

    def _set_projection(self, projection: ProjectionMode, *, persist: bool) -> None:
        del persist
        flags = _SDC_APPLY | _TOPOLOGY_TO_FLAG[projection] | _SDC_ALLOW_CHANGES
        result = int(self._user32.SetDisplayConfig(0, None, 0, None, flags))
        if result != 0:
            raise DisplayConfigurationError(
                f"Windows could not apply projection mode {projection.label} (result {result})"
            )


def create_display_configuration_backend() -> DisplayConfigurationBackend:
    """Return the native backend when available, otherwise a safe no-op capability backend."""

    if sys.platform != "win32":
        return UnavailableDisplayConfigurationBackend()
    try:
        return WindowsDisplayConfigurationBackend()
    except OSError:
        return UnavailableDisplayConfigurationBackend()
