"""Failure-isolated Windows saved-profile Wi-Fi control boundary for Vigil."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

from vigil_overlay.services.wifi_connection_identity import (
    ConnectedWifiIdentity,
    ConnectedWifiIdentityResolver,
    create_platform_connected_wifi_identity_resolver,
)

_LOGGER = logging.getLogger("vigil_overlay")

_ERROR_SUCCESS: Final[int] = 0
_ERROR_ACCESS_DENIED: Final[int] = 5
_WLAN_API_VERSION_2_0: Final[int] = 2
_WLAN_CONNECTION_MODE_PROFILE: Final[int] = 0
_DOT11_BSS_TYPE_INFRASTRUCTURE: Final[int] = 1
_WLAN_INTERFACE_STATE_CONNECTED: Final[int] = 1
_WLAN_INTF_OPCODE_RADIO_STATE: Final[int] = 4
_DOT11_RADIO_STATE_ON: Final[int] = 1
_DOT11_RADIO_STATE_OFF: Final[int] = 2
_WLAN_MAX_PHY_INDEX: Final[int] = 64
_MAX_PROFILES: Final[int] = 256


class WifiControlError(RuntimeError):
    """Raised when a requested Wi-Fi operation cannot be completed."""


@dataclass(frozen=True, slots=True)
class WifiProfileInfo:
    """One Windows-managed Wi-Fi profile available on the selected adapter."""

    interface_id: str
    interface_name: str
    profile_name: str
    ssid: str | None = None


@dataclass(frozen=True, slots=True)
class WifiSnapshot:
    """Location-free Windows Wi-Fi state consumed by Vigil's Wi-Fi widget."""

    available: bool
    detail: str
    interface_name: str | None = None
    connected: bool = False
    wifi_enabled: bool | None = None
    connected_profile_name: str | None = None
    profiles: tuple[WifiProfileInfo, ...] = ()


class WifiControlBackend(Protocol):
    """Backend contract used by the controller-first Wi-Fi widget."""

    @property
    def available(self) -> bool: ...

    def snapshot(self) -> WifiSnapshot: ...

    def connect(self, profile: WifiProfileInfo) -> None: ...

    def disconnect(self) -> None: ...

    def set_wifi_enabled(self, enabled: bool) -> None: ...

    def open_wifi_settings(self) -> None: ...


class UnavailableWifiControlBackend:
    """Non-Windows/failure-isolated backend."""

    def __init__(self, detail: str = "Windows Wi-Fi controls are unavailable.") -> None:
        self._detail = detail

    @property
    def available(self) -> bool:
        return False

    def snapshot(self) -> WifiSnapshot:
        return WifiSnapshot(False, self._detail)

    def connect(self, profile: WifiProfileInfo) -> None:
        del profile
        raise WifiControlError(self._detail)

    def disconnect(self) -> None:
        raise WifiControlError(self._detail)

    def set_wifi_enabled(self, enabled: bool) -> None:
        del enabled
        raise WifiControlError(self._detail)

    def open_wifi_settings(self) -> None:
        raise WifiControlError(self._detail)


class _Guid(ctypes.Structure):
    _fields_ = (
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    )


class _WlanInterfaceInfo(ctypes.Structure):
    _fields_ = (
        ("guid", _Guid),
        ("description", wintypes.WCHAR * 256),
        ("state", wintypes.DWORD),
    )


class _WlanProfileInfo(ctypes.Structure):
    _fields_ = (
        ("profile_name", wintypes.WCHAR * 256),
        ("flags", wintypes.DWORD),
    )


class _WlanPhyRadioState(ctypes.Structure):
    _fields_ = (
        ("phy_index", wintypes.DWORD),
        ("software_state", wintypes.DWORD),
        ("hardware_state", wintypes.DWORD),
    )


class _WlanRadioState(ctypes.Structure):
    _fields_ = (
        ("number_of_phys", wintypes.DWORD),
        ("phy_states", _WlanPhyRadioState * _WLAN_MAX_PHY_INDEX),
    )


class _WlanConnectionParameters(ctypes.Structure):
    _fields_ = (
        ("connection_mode", wintypes.DWORD),
        ("profile", wintypes.LPCWSTR),
        ("ssid", ctypes.c_void_p),
        ("desired_bssid_list", ctypes.c_void_p),
        ("bss_type", wintypes.DWORD),
        ("flags", wintypes.DWORD),
    )


@dataclass(frozen=True, slots=True)
class _InterfaceInfo:
    interface_id: str
    name: str
    state: int


class NativeWifiControlBackend:
    """Windows Native Wi-Fi backend limited to Windows-managed saved profiles."""

    def __init__(
        self,
        connected_identity_resolver: ConnectedWifiIdentityResolver | None = None,
    ) -> None:
        if os.name != "nt":
            raise WifiControlError("Wi-Fi controls are available on Windows only.")
        windll_type = cast(Any, ctypes).WinDLL
        try:
            self._wlan = windll_type("wlanapi", use_last_error=True)
        except OSError as exc:
            raise WifiControlError(
                "Windows Native Wi-Fi service is unavailable."
            ) from exc
        self._connected_identity_resolver = (
            connected_identity_resolver
            or create_platform_connected_wifi_identity_resolver()
        )
        self._configure_api()

    @property
    def available(self) -> bool:
        return True

    def snapshot(self) -> WifiSnapshot:
        with self._client() as handle:
            interfaces = self._interfaces(handle)
            if not interfaces:
                return WifiSnapshot(False, "No Wi-Fi adapter is available.")
            interface = next(
                (
                    item
                    for item in interfaces
                    if item.state == _WLAN_INTERFACE_STATE_CONNECTED
                ),
                interfaces[0],
            )
            profiles = self._profiles(handle, interface)
            wifi_enabled = self._wifi_enabled(handle, interface)
            connected_identities = (
                self._connected_identity_resolver.resolve()
                if interface.state == _WLAN_INTERFACE_STATE_CONNECTED
                else ()
            )
            connected_profile_name = _match_connected_profile_name(
                interface.interface_id,
                profiles,
                connected_identities,
            )
            return WifiSnapshot(
                True,
                "Windows-managed saved Wi-Fi profiles are available.",
                interface_name=interface.name,
                connected=interface.state == _WLAN_INTERFACE_STATE_CONNECTED,
                wifi_enabled=wifi_enabled,
                connected_profile_name=connected_profile_name,
                profiles=profiles,
            )

    def connect(self, profile: WifiProfileInfo) -> None:
        with self._client() as handle:
            guid = _guid_from_string(profile.interface_id)
            params = _WlanConnectionParameters(
                _WLAN_CONNECTION_MODE_PROFILE,
                profile.profile_name,
                None,
                None,
                _DOT11_BSS_TYPE_INFRASTRUCTURE,
                0,
            )
            status = int(
                self._wlan.WlanConnect(
                    handle, ctypes.byref(guid), ctypes.byref(params), None
                )
            )
            self._check_status(
                status,
                f"connect using the saved Wi-Fi profile '{profile.profile_name}'",
            )

    def disconnect(self) -> None:
        with self._client() as handle:
            interfaces = self._interfaces(handle)
            connected = [
                item
                for item in interfaces
                if item.state == _WLAN_INTERFACE_STATE_CONNECTED
            ]
            if not connected:
                return
            for interface in connected:
                guid = _guid_from_string(interface.interface_id)
                status = int(
                    self._wlan.WlanDisconnect(handle, ctypes.byref(guid), None)
                )
                self._check_status(status, "disconnect the Wi-Fi interface")

    def set_wifi_enabled(self, enabled: bool) -> None:
        with self._client() as handle:
            interfaces = self._interfaces(handle)
            if not interfaces:
                raise WifiControlError("No Wi-Fi adapter is available.")
            for interface in interfaces:
                self._set_interface_radio_state(handle, interface, enabled=enabled)

    def open_wifi_settings(self) -> None:
        _open_settings_uri("ms-settings:network-wifi")

    def _configure_api(self) -> None:
        self._wlan.WlanOpenHandle.argtypes = (
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.HANDLE),
        )
        self._wlan.WlanOpenHandle.restype = wintypes.DWORD
        self._wlan.WlanCloseHandle.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
        self._wlan.WlanCloseHandle.restype = wintypes.DWORD
        self._wlan.WlanEnumInterfaces.argtypes = (
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._wlan.WlanEnumInterfaces.restype = wintypes.DWORD
        self._wlan.WlanGetProfileList.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_Guid),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._wlan.WlanGetProfileList.restype = wintypes.DWORD
        self._wlan.WlanGetProfile.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_Guid),
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        )
        self._wlan.WlanGetProfile.restype = wintypes.DWORD
        self._wlan.WlanConnect.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_Guid),
            ctypes.POINTER(_WlanConnectionParameters),
            ctypes.c_void_p,
        )
        self._wlan.WlanConnect.restype = wintypes.DWORD
        self._wlan.WlanDisconnect.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_Guid),
            ctypes.c_void_p,
        )
        self._wlan.WlanDisconnect.restype = wintypes.DWORD
        self._wlan.WlanQueryInterface.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_Guid),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        )
        self._wlan.WlanQueryInterface.restype = wintypes.DWORD
        self._wlan.WlanSetInterface.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_Guid),
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._wlan.WlanSetInterface.restype = wintypes.DWORD
        self._wlan.WlanFreeMemory.argtypes = (ctypes.c_void_p,)
        self._wlan.WlanFreeMemory.restype = None

    @contextmanager
    def _client(self) -> Iterator[wintypes.HANDLE]:
        negotiated = wintypes.DWORD(0)
        handle = wintypes.HANDLE()
        status = int(
            self._wlan.WlanOpenHandle(
                _WLAN_API_VERSION_2_0,
                None,
                ctypes.byref(negotiated),
                ctypes.byref(handle),
            )
        )
        self._check_status(status, "open the Windows Wi-Fi service")
        try:
            yield handle
        finally:
            self._wlan.WlanCloseHandle(handle, None)

    def _interfaces(self, handle: wintypes.HANDLE) -> tuple[_InterfaceInfo, ...]:
        pointer = ctypes.c_void_p()
        status = int(self._wlan.WlanEnumInterfaces(handle, None, ctypes.byref(pointer)))
        self._check_status(status, "enumerate Wi-Fi adapters")
        if not pointer.value:
            return ()
        try:
            count = int(ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))[0])
            if count <= 0:
                return ()
            array_type = _WlanInterfaceInfo * count
            address = int(pointer.value) + ctypes.sizeof(wintypes.DWORD) * 2
            entries = cast(Any, array_type.from_address(address))
            return tuple(
                _InterfaceInfo(
                    _guid_to_string(entry.guid),
                    str(entry.description).rstrip("\x00"),
                    int(entry.state),
                )
                for entry in entries
            )
        finally:
            self._wlan.WlanFreeMemory(pointer)

    def _wifi_enabled(
        self,
        handle: wintypes.HANDLE,
        interface: _InterfaceInfo,
    ) -> bool | None:
        guid = _guid_from_string(interface.interface_id)
        data_size = wintypes.DWORD(0)
        pointer = ctypes.c_void_p()
        value_type = wintypes.DWORD(0)
        status = int(
            self._wlan.WlanQueryInterface(
                handle,
                ctypes.byref(guid),
                _WLAN_INTF_OPCODE_RADIO_STATE,
                None,
                ctypes.byref(data_size),
                ctypes.byref(pointer),
                ctypes.byref(value_type),
            )
        )
        if status != _ERROR_SUCCESS or not pointer.value:
            return None
        try:
            radio_state = ctypes.cast(pointer, ctypes.POINTER(_WlanRadioState)).contents
            count = min(max(int(radio_state.number_of_phys), 0), _WLAN_MAX_PHY_INDEX)
            if count == 0:
                return None
            return any(
                int(radio_state.phy_states[index].software_state)
                == _DOT11_RADIO_STATE_ON
                and int(radio_state.phy_states[index].hardware_state)
                == _DOT11_RADIO_STATE_ON
                for index in range(count)
            )
        finally:
            self._wlan.WlanFreeMemory(pointer)

    def _set_interface_radio_state(
        self,
        handle: wintypes.HANDLE,
        interface: _InterfaceInfo,
        *,
        enabled: bool,
    ) -> None:
        guid = _guid_from_string(interface.interface_id)
        states = self._radio_phy_states(handle, interface)
        if not states:
            raise WifiControlError(
                f"Windows did not report a controllable Wi-Fi radio for '{interface.name}'."
            )
        target = _DOT11_RADIO_STATE_ON if enabled else _DOT11_RADIO_STATE_OFF
        for state in states:
            requested = _WlanPhyRadioState(
                state.phy_index,
                target,
                state.hardware_state,
            )
            status = int(
                self._wlan.WlanSetInterface(
                    handle,
                    ctypes.byref(guid),
                    _WLAN_INTF_OPCODE_RADIO_STATE,
                    ctypes.sizeof(requested),
                    ctypes.byref(requested),
                    None,
                )
            )
            self._check_status(
                status,
                f"turn Wi-Fi {'on' if enabled else 'off'} for '{interface.name}'",
            )

    def _radio_phy_states(
        self,
        handle: wintypes.HANDLE,
        interface: _InterfaceInfo,
    ) -> tuple[_WlanPhyRadioState, ...]:
        guid = _guid_from_string(interface.interface_id)
        data_size = wintypes.DWORD(0)
        pointer = ctypes.c_void_p()
        value_type = wintypes.DWORD(0)
        status = int(
            self._wlan.WlanQueryInterface(
                handle,
                ctypes.byref(guid),
                _WLAN_INTF_OPCODE_RADIO_STATE,
                None,
                ctypes.byref(data_size),
                ctypes.byref(pointer),
                ctypes.byref(value_type),
            )
        )
        self._check_status(status, "read the Wi-Fi radio state")
        if not pointer.value:
            return ()
        try:
            radio_state = ctypes.cast(pointer, ctypes.POINTER(_WlanRadioState)).contents
            count = min(max(int(radio_state.number_of_phys), 0), _WLAN_MAX_PHY_INDEX)
            return tuple(
                _WlanPhyRadioState(
                    int(radio_state.phy_states[index].phy_index),
                    int(radio_state.phy_states[index].software_state),
                    int(radio_state.phy_states[index].hardware_state),
                )
                for index in range(count)
            )
        finally:
            self._wlan.WlanFreeMemory(pointer)

    def _profiles(
        self,
        handle: wintypes.HANDLE,
        interface: _InterfaceInfo,
    ) -> tuple[WifiProfileInfo, ...]:
        guid = _guid_from_string(interface.interface_id)
        pointer = ctypes.c_void_p()
        status = int(
            self._wlan.WlanGetProfileList(
                handle,
                ctypes.byref(guid),
                None,
                ctypes.byref(pointer),
            )
        )
        self._check_status(status, "read saved Wi-Fi profiles")
        if not pointer.value:
            return ()
        try:
            count = int(ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))[0])
            count = min(max(count, 0), _MAX_PROFILES)
            if count == 0:
                return ()
            array_type = _WlanProfileInfo * count
            address = int(pointer.value) + ctypes.sizeof(wintypes.DWORD) * 2
            entries = cast(Any, array_type.from_address(address))
            profiles: list[WifiProfileInfo] = []
            seen: set[str] = set()
            for entry in entries:
                name = str(entry.profile_name).rstrip("\x00").strip()
                if not name or name.casefold() in seen:
                    continue
                seen.add(name.casefold())
                profiles.append(
                    WifiProfileInfo(
                        interface_id=interface.interface_id,
                        interface_name=interface.name,
                        profile_name=name,
                        ssid=self._profile_ssid(handle, interface, name),
                    )
                )
            return tuple(profiles)
        finally:
            self._wlan.WlanFreeMemory(pointer)

    def _profile_ssid(
        self,
        handle: wintypes.HANDLE,
        interface: _InterfaceInfo,
        profile_name: str,
    ) -> str | None:
        guid = _guid_from_string(interface.interface_id)
        xml_pointer = ctypes.c_void_p()
        flags = wintypes.DWORD(0)
        access = wintypes.DWORD(0)
        status = int(
            self._wlan.WlanGetProfile(
                handle,
                ctypes.byref(guid),
                profile_name,
                None,
                ctypes.byref(xml_pointer),
                ctypes.byref(flags),
                ctypes.byref(access),
            )
        )
        if status != _ERROR_SUCCESS or not xml_pointer.value:
            _LOGGER.debug(
                "Windows could not read saved Wi-Fi profile metadata for %s (error %d)",
                profile_name,
                status,
            )
            return None
        try:
            profile_xml = ctypes.cast(xml_pointer, wintypes.LPCWSTR).value
            return _ssid_from_profile_xml(profile_xml or "")
        finally:
            self._wlan.WlanFreeMemory(xml_pointer)

    @staticmethod
    def _check_status(status: int, action: str) -> None:
        if status == _ERROR_SUCCESS:
            return
        if status == _ERROR_ACCESS_DENIED:
            raise WifiControlError(f"Windows denied permission to {action}.")
        raise WifiControlError(f"Windows could not {action} (error {status}).")


def create_platform_wifi_control_backend() -> WifiControlBackend:
    """Create the Windows Native Wi-Fi backend without making it a startup dependency."""

    if sys.platform != "win32" or os.name != "nt":
        return UnavailableWifiControlBackend(
            "Wi-Fi controls are available on Windows only."
        )
    try:
        return NativeWifiControlBackend()
    except WifiControlError as exc:
        _LOGGER.warning("Windows Wi-Fi backend unavailable: %s", exc)
        return UnavailableWifiControlBackend(str(exc))


def _match_connected_profile_name(
    interface_id: str,
    profiles: tuple[WifiProfileInfo, ...],
    identities: tuple[ConnectedWifiIdentity, ...],
) -> str | None:
    """Return one trustworthy saved-profile match for the connected WLAN."""

    normalized_interface = _normalize_adapter_id(interface_id)
    interface_identities = tuple(
        identity
        for identity in identities
        if identity.adapter_id is not None
        and _normalize_adapter_id(identity.adapter_id) == normalized_interface
    )
    candidates = (
        interface_identities
        if interface_identities
        else identities if len(identities) == 1 else ()
    )
    interface_profiles = tuple(
        profile
        for profile in profiles
        if _normalize_adapter_id(profile.interface_id) == normalized_interface
    )

    for identity in candidates:
        if not identity.profile_name:
            continue
        normalized_name = identity.profile_name.casefold()
        name_matches = tuple(
            profile
            for profile in interface_profiles
            if profile.profile_name.casefold() == normalized_name
        )
        if len(name_matches) == 1:
            return name_matches[0].profile_name

    for identity in candidates:
        if not identity.ssid:
            continue
        normalized_ssid = identity.ssid.casefold()
        ssid_matches = tuple(
            profile
            for profile in interface_profiles
            if profile.ssid is not None and profile.ssid.casefold() == normalized_ssid
        )
        if len(ssid_matches) == 1:
            return ssid_matches[0].profile_name
    return None


def _ssid_from_profile_xml(profile_xml: str) -> str | None:
    if not profile_xml.strip():
        return None
    try:
        root = ET.fromstring(profile_xml)
    except ET.ParseError:
        return None
    for element in root.iter():
        if _xml_local_name(element.tag) != "SSID":
            continue
        for child in element:
            child_name = _xml_local_name(child.tag)
            text = (child.text or "").strip()
            if child_name == "name" and text:
                return text
            if child_name == "hex" and text:
                try:
                    return bytes.fromhex(text).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    return None
    return None


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _normalize_adapter_id(value: str) -> str:
    return value.strip().strip("{}").casefold()


def _guid_to_string(value: _Guid) -> str:
    raw = ctypes.string_at(ctypes.byref(value), ctypes.sizeof(_Guid))
    return str(uuid.UUID(bytes_le=raw))


def _guid_from_string(value: str) -> _Guid:
    return _Guid.from_buffer_copy(uuid.UUID(value).bytes_le)


def _open_settings_uri(uri: str) -> None:
    if os.name != "nt" or not hasattr(os, "startfile"):
        raise WifiControlError("Windows Settings can only be opened on Windows.")
    try:
        os.startfile(uri)
    except OSError as exc:
        raise WifiControlError("Could not open Windows Settings.") from exc
