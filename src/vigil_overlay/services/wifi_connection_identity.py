"""Location-independent connected WLAN identity through Windows Runtime."""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final, Protocol, cast

_LOGGER = logging.getLogger("vigil_overlay")
_DEFAULT_QUERY_TIMEOUT_SECONDS: Final[float] = 0.1


@dataclass(frozen=True, slots=True)
class ConnectedWifiIdentity:
    """Connected WLAN identity returned independently of saved-profile control."""

    adapter_id: str | None
    profile_name: str | None
    ssid: str | None


class ConnectedWifiIdentityResolver(Protocol):
    """Resolve connected WLAN identities without using location-gated WLAN queries."""

    def resolve(self) -> tuple[ConnectedWifiIdentity, ...]: ...


class UnavailableConnectedWifiIdentityResolver:
    """Safe resolver used when the Windows Runtime projection is unavailable."""

    def resolve(self) -> tuple[ConnectedWifiIdentity, ...]:
        return ()


class WinRtConnectedWifiIdentityResolver:
    """Query connected WLAN profiles on a bounded dedicated MTA worker thread."""

    def __init__(
        self,
        *,
        query_timeout_seconds: float = _DEFAULT_QUERY_TIMEOUT_SECONDS,
        query: Callable[[], tuple[ConnectedWifiIdentity, ...]] | None = None,
    ) -> None:
        if query_timeout_seconds <= 0:
            raise ValueError("query_timeout_seconds must be positive")
        self._query_timeout_seconds = query_timeout_seconds
        self._query = query or _query_connected_wifi_identities
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._result: tuple[ConnectedWifiIdentity, ...] = ()
        self._query_failed = False
        self._last_error: str | None = None

    def resolve(self) -> tuple[ConnectedWifiIdentity, ...]:
        with self._lock:
            worker = self._worker
            if worker is not None:
                if worker.is_alive():
                    return ()
                return self._consume_result_locked()
            worker = threading.Thread(
                target=self._run_query,
                name="VigilWifiIdentity",
                daemon=True,
            )
            self._worker = worker
            self._result = ()
            self._query_failed = False
            worker.start()

        worker.join(self._query_timeout_seconds)
        with self._lock:
            if worker.is_alive():
                if self._last_error != "timeout":
                    _LOGGER.warning(
                        "Connected Wi-Fi identity query exceeded %.1f seconds; "
                        "reporting connected identity as unresolved",
                        self._query_timeout_seconds,
                    )
                    self._last_error = "timeout"
                return ()
            return self._consume_result_locked()

    def _consume_result_locked(self) -> tuple[ConnectedWifiIdentity, ...]:
        result = () if self._query_failed else self._result
        self._worker = None
        self._result = ()
        self._query_failed = False
        return result

    def _run_query(self) -> None:
        try:
            resolved = self._query()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            with self._lock:
                if detail != self._last_error:
                    _LOGGER.warning(
                        "Connected Wi-Fi identity could not be resolved without changing "
                        "saved-profile control behavior: %s",
                        detail,
                    )
                    self._last_error = detail
                self._query_failed = True
                self._result = ()
            return
        with self._lock:
            self._result = resolved
            self._query_failed = False
            self._last_error = None


def create_platform_connected_wifi_identity_resolver() -> ConnectedWifiIdentityResolver:
    """Create the WinRT resolver on Windows or a safe no-op elsewhere."""

    import os
    import sys

    if sys.platform == "win32" and os.name == "nt":
        return WinRtConnectedWifiIdentityResolver()
    return UnavailableConnectedWifiIdentityResolver()


def _query_connected_wifi_identities() -> tuple[ConnectedWifiIdentity, ...]:
    runtime = importlib.import_module("winrt.runtime")
    connectivity = importlib.import_module("winrt.windows.networking.connectivity")
    apartment_type = cast(Any, runtime).ApartmentType.MULTI_THREADED
    cast(Any, runtime).init_apartment(apartment_type)
    try:
        return asyncio.run(_find_connected_wifi_identities(connectivity))
    finally:
        cast(Any, runtime).uninit_apartment()


async def _find_connected_wifi_identities(
    connectivity: ModuleType,
) -> tuple[ConnectedWifiIdentity, ...]:
    namespace = cast(Any, connectivity)
    profile_filter = namespace.ConnectionProfileFilter()
    profile_filter.is_wlan_connection_profile = True
    profile_filter.is_connected = True
    profiles = await namespace.NetworkInformation.find_connection_profiles_async(
        profile_filter
    )
    identities: list[ConnectedWifiIdentity] = []
    for profile in profiles:
        profile_name = _clean_text(getattr(profile, "profile_name", None))
        details = getattr(profile, "wlan_connection_profile_details", None)
        ssid = (
            _clean_text(details.get_connected_ssid()) if details is not None else None
        )
        network_adapter = getattr(profile, "network_adapter", None)
        adapter_id = _clean_text(
            getattr(network_adapter, "network_adapter_id", None)
            if network_adapter is not None
            else None
        )
        identities.append(
            ConnectedWifiIdentity(
                adapter_id=adapter_id,
                profile_name=profile_name,
                ssid=ssid,
            )
        )
    return tuple(identities)


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
