"""Native Windows telemetry matching Task Manager-style system metrics.

The collector is read-only and uses GetSystemTimes, GlobalMemoryStatusEx,
CallNtPowerInformation, DXGI adapter descriptions, and PDH GPU counters.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast

from vigil_overlay.services.telemetry import RawTelemetrySample

_LOGGER = logging.getLogger("vigil_overlay")
_ERROR_SUCCESS: Final[int] = 0
_PDH_MORE_DATA: Final[int] = 0x800007D2
_PDH_FMT_DOUBLE: Final[int] = 0x00000200
_PDH_CSTATUS_VALID_DATA: Final[int] = 0x00000000
_PDH_CSTATUS_NEW_DATA: Final[int] = 0x00000001
_DXGI_ERROR_NOT_FOUND: Final[int] = 0x887A0002
_PROCESSOR_INFORMATION_LEVEL: Final[int] = 11

_PROCESS_ENGINE_RE = re.compile(
    r"(?:^|_)pid_(?P<pid>\d+)_luid_0x(?P<high>[0-9a-f]+)_0x(?P<low>[0-9a-f]+)_"
    r"phys_(?P<phys>\d+)_eng_(?P<engine>\d+)_engtype_(?P<type>[^_]+)",
    re.IGNORECASE,
)
_ENGINE_RE = re.compile(
    r"luid_0x(?P<high>[0-9a-f]+)_0x(?P<low>[0-9a-f]+)_"
    r"phys_(?P<phys>\d+)_eng_(?P<engine>\d+)_engtype_(?P<type>[^_]+)",
    re.IGNORECASE,
)
_LUID_RE = re.compile(
    r"luid_0x(?P<high>[0-9a-f]+)_0x(?P<low>[0-9a-f]+)",
    re.IGNORECASE,
)

LuidKey = tuple[int, int]


@dataclass(frozen=True, slots=True)
class GpuAdapterInfo:
    """Static DXGI identity and dedicated-memory capacity for one adapter."""

    luid: LuidKey
    name: str
    dedicated_bytes: int


@dataclass(frozen=True, slots=True)
class GpuReading:
    """Normalized GPU utilization, identity, and dedicated-memory reading."""

    utilization_percent: float | None = None
    adapter_name: str = ""
    dedicated_used_bytes: int | None = None
    dedicated_total_bytes: int | None = None


class WindowsTelemetrySampler:
    """Collect one-second Windows system telemetry without writing any user data."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("WindowsTelemetrySampler is only available on Windows")
        self._cpu: _CpuUsageSampler = _CpuUsageSampler()
        self._memory: _MemorySampler = _MemorySampler()
        self._frequency: _CpuFrequencySampler = _CpuFrequencySampler()
        try:
            self._gpu: _GpuTelemetrySampler | _UnavailableGpuSampler = (
                _GpuTelemetrySampler()
            )
        except OSError:
            _LOGGER.exception(
                "Windows GPU counters are unavailable; CPU and RAM remain active"
            )
            self._gpu = _UnavailableGpuSampler()
        self._closed: bool = False

    def sample(self) -> RawTelemetrySample:
        if self._closed:
            raise RuntimeError("telemetry sampler is closed")
        cpu_percent: float | None = None
        cpu_frequency_ghz: float | None = None
        memory: _MemoryReading | None = None
        gpu = GpuReading()
        try:
            cpu_percent = self._cpu.sample_percent()
        except OSError:
            _LOGGER.exception("Windows CPU telemetry sample failed")
        try:
            cpu_frequency_ghz = self._frequency.sample_ghz()
        except OSError:
            _LOGGER.exception("Windows CPU frequency sample failed")
        try:
            memory = self._memory.sample()
        except OSError:
            _LOGGER.exception("Windows memory telemetry sample failed")
        try:
            gpu = self._gpu.sample()
        except OSError:
            _LOGGER.exception("Windows GPU telemetry sample failed")
        vram_percent = _percentage(gpu.dedicated_used_bytes, gpu.dedicated_total_bytes)
        return RawTelemetrySample(
            captured_at_utc=datetime.now(UTC),
            cpu_percent=cpu_percent,
            cpu_frequency_ghz=cpu_frequency_ghz,
            gpu_percent=gpu.utilization_percent,
            gpu_name=gpu.adapter_name,
            vram_percent=vram_percent,
            vram_used_bytes=gpu.dedicated_used_bytes,
            vram_total_bytes=gpu.dedicated_total_bytes,
            ram_percent=(memory.percent if memory is not None else None),
            ram_used_bytes=(memory.used_bytes if memory is not None else None),
            ram_total_bytes=(memory.total_bytes if memory is not None else None),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._gpu.close()
        self._closed = True


@dataclass(frozen=True, slots=True)
class _MemoryReading:
    percent: float
    used_bytes: int
    total_bytes: int


class _FILETIME(ctypes.Structure):
    _fields_ = (("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD))


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = (
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    )


class _PROCESSOR_POWER_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("Number", wintypes.ULONG),
        ("MaxMhz", wintypes.ULONG),
        ("CurrentMhz", wintypes.ULONG),
        ("MhzLimit", wintypes.ULONG),
        ("MaxIdleState", wintypes.ULONG),
        ("CurrentIdleState", wintypes.ULONG),
    )


class _LUID(ctypes.Structure):
    _fields_ = (("LowPart", wintypes.DWORD), ("HighPart", ctypes.c_int32))


class _GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    )


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = (
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", wintypes.UINT),
    )


class _PDH_FMT_COUNTERVALUE_UNION(ctypes.Union):
    _fields_ = (
        ("longValue", ctypes.c_long),
        ("doubleValue", ctypes.c_double),
        ("largeValue", ctypes.c_longlong),
        ("AnsiStringValue", ctypes.c_char_p),
        ("WideStringValue", wintypes.LPWSTR),
    )


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = (("CStatus", wintypes.DWORD), ("value", _PDH_FMT_COUNTERVALUE_UNION))


class _PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = (("szName", wintypes.LPWSTR), ("FmtValue", _PDH_FMT_COUNTERVALUE))


class _CpuUsageSampler:
    def __init__(self) -> None:
        kernel32 = _load_windll("kernel32")
        get_system_times = kernel32.GetSystemTimes
        get_system_times.argtypes = (
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        )
        get_system_times.restype = wintypes.BOOL
        self._get_system_times = get_system_times
        self._previous = self._read_times()

    def sample_percent(self) -> float | None:
        current = self._read_times()
        result = calculate_cpu_percent(self._previous, current)
        self._previous = current
        return result

    def _read_times(self) -> tuple[int, int, int]:
        idle = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        if not self._get_system_times(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise _win_error()
        return (_filetime_value(idle), _filetime_value(kernel), _filetime_value(user))


class _MemorySampler:
    def __init__(self) -> None:
        kernel32 = _load_windll("kernel32")
        memory_status = kernel32.GlobalMemoryStatusEx
        memory_status.argtypes = (ctypes.POINTER(_MEMORYSTATUSEX),)
        memory_status.restype = wintypes.BOOL
        self._memory_status = memory_status

    def sample(self) -> _MemoryReading:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not self._memory_status(ctypes.byref(status)):
            raise _win_error()
        total = int(status.ullTotalPhys)
        available = int(status.ullAvailPhys)
        used = max(total - available, 0)
        return _MemoryReading(
            percent=_clamp_percent(float(status.dwMemoryLoad)),
            used_bytes=used,
            total_bytes=total,
        )


class _CpuFrequencySampler:
    def __init__(self) -> None:
        powrprof = _load_windll("powrprof")
        call_nt_power = powrprof.CallNtPowerInformation
        call_nt_power.argtypes = (
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        )
        call_nt_power.restype = wintypes.ULONG
        self._call_nt_power = call_nt_power
        self._processor_count = max(os.cpu_count() or 1, 1)

    def sample_ghz(self) -> float | None:
        buffer_type = _PROCESSOR_POWER_INFORMATION * self._processor_count
        buffer = buffer_type()
        status = int(
            self._call_nt_power(
                _PROCESSOR_INFORMATION_LEVEL,
                None,
                0,
                ctypes.byref(buffer),
                ctypes.sizeof(buffer),
            )
        )
        if status != _ERROR_SUCCESS:
            return None
        values = [int(item.CurrentMhz) for item in buffer if item.CurrentMhz > 0]
        if not values:
            return None
        return sum(values) / len(values) / 1000.0


class _UnavailableGpuSampler:
    def sample(self) -> GpuReading:
        return GpuReading()

    def close(self) -> None:
        return


class _GpuTelemetrySampler:
    def __init__(self) -> None:
        self._adapters = _enumerate_dxgi_adapters()
        self._query = _PdhGpuQuery()

    def sample(self) -> GpuReading:
        engine_items, memory_items = self._query.collect()
        utilization_by_adapter = aggregate_gpu_engine_usage(engine_items)
        memory_by_adapter = aggregate_adapter_memory(memory_items)
        selected = select_gpu_adapter(
            self._adapters, utilization_by_adapter, memory_by_adapter
        )
        if selected is None:
            return GpuReading()
        utilization = utilization_by_adapter.get(selected.luid)
        used = memory_by_adapter.get(selected.luid)
        return GpuReading(
            utilization_percent=utilization,
            adapter_name=selected.name,
            dedicated_used_bytes=used,
            dedicated_total_bytes=(selected.dedicated_bytes or None),
        )

    def close(self) -> None:
        self._query.close()


class _PdhGpuQuery:
    ENGINE_COUNTER = r"\GPU Engine(*)\Utilization Percentage"
    MEMORY_COUNTER = r"\GPU Adapter Memory(*)\Dedicated Usage"

    def __init__(self) -> None:
        self._pdh = _load_windll("pdh")
        self._configure_functions()
        self._query = ctypes.c_void_p()
        self._engine_counter = ctypes.c_void_p()
        self._memory_counter = ctypes.c_void_p()
        self._closed = False
        status = int(self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)))
        if status != _ERROR_SUCCESS:
            raise OSError(f"PdhOpenQueryW failed: 0x{status & 0xFFFFFFFF:08X}")
        try:
            self._add_counter(self.ENGINE_COUNTER, self._engine_counter)
            self._add_counter(self.MEMORY_COUNTER, self._memory_counter)
            # Prime rate-based counters. The worker waits one second before the next collect.
            self._pdh.PdhCollectQueryData(self._query)
        except Exception:
            self.close()
            raise

    def collect(
        self,
    ) -> tuple[tuple[tuple[str, float], ...], tuple[tuple[str, float], ...]]:
        if self._closed:
            return (), ()
        status = int(self._pdh.PdhCollectQueryData(self._query))
        if status != _ERROR_SUCCESS:
            _LOGGER.debug(
                "PdhCollectQueryData unavailable: 0x%08X", status & 0xFFFFFFFF
            )
            return (), ()
        return (
            self._formatted_array(self._engine_counter),
            self._formatted_array(self._memory_counter),
        )

    def close(self) -> None:
        if self._closed:
            return
        if self._query.value:
            self._pdh.PdhCloseQuery(self._query)
        self._query = ctypes.c_void_p()
        self._closed = True

    def _configure_functions(self) -> None:
        self._pdh.PdhOpenQueryW.argtypes = (
            wintypes.LPCWSTR,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._pdh.PdhOpenQueryW.restype = wintypes.ULONG
        self._pdh.PdhAddEnglishCounterW.argtypes = (
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._pdh.PdhAddEnglishCounterW.restype = wintypes.ULONG
        self._pdh.PdhCollectQueryData.argtypes = (ctypes.c_void_p,)
        self._pdh.PdhCollectQueryData.restype = wintypes.ULONG
        self._pdh.PdhGetFormattedCounterArrayW.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        )
        self._pdh.PdhGetFormattedCounterArrayW.restype = wintypes.ULONG
        self._pdh.PdhCloseQuery.argtypes = (ctypes.c_void_p,)
        self._pdh.PdhCloseQuery.restype = wintypes.ULONG

    def _add_counter(self, path: str, output: ctypes.c_void_p) -> None:
        status = int(
            self._pdh.PdhAddEnglishCounterW(
                self._query,
                path,
                0,
                ctypes.byref(output),
            )
        )
        if status != _ERROR_SUCCESS:
            raise OSError(
                f"PdhAddEnglishCounterW({path}) failed: 0x{status & 0xFFFFFFFF:08X}"
            )

    def _formatted_array(
        self, counter: ctypes.c_void_p
    ) -> tuple[tuple[str, float], ...]:
        buffer_size = wintypes.DWORD(0)
        item_count = wintypes.DWORD(0)
        status = int(
            self._pdh.PdhGetFormattedCounterArrayW(
                counter,
                _PDH_FMT_DOUBLE,
                ctypes.byref(buffer_size),
                ctypes.byref(item_count),
                None,
            )
        )
        if status not in {_PDH_MORE_DATA, _ERROR_SUCCESS} or buffer_size.value == 0:
            return ()
        buffer = ctypes.create_string_buffer(buffer_size.value)
        status = int(
            self._pdh.PdhGetFormattedCounterArrayW(
                counter,
                _PDH_FMT_DOUBLE,
                ctypes.byref(buffer_size),
                ctypes.byref(item_count),
                ctypes.cast(buffer, ctypes.c_void_p),
            )
        )
        if status != _ERROR_SUCCESS:
            return ()
        items = ctypes.cast(buffer, ctypes.POINTER(_PDH_FMT_COUNTERVALUE_ITEM_W))
        result: list[tuple[str, float]] = []
        for index in range(item_count.value):
            item = items[index]
            if item.FmtValue.CStatus not in {
                _PDH_CSTATUS_VALID_DATA,
                _PDH_CSTATUS_NEW_DATA,
            }:
                continue
            name = item.szName or ""
            result.append((name, float(item.FmtValue.doubleValue)))
        return tuple(result)


def calculate_cpu_percent(
    previous: tuple[int, int, int],
    current: tuple[int, int, int],
) -> float | None:
    """Calculate busy CPU percentage from idle/kernel/user cumulative FILETIME values."""

    idle_delta = current[0] - previous[0]
    kernel_delta = current[1] - previous[1]
    user_delta = current[2] - previous[2]
    total_delta = kernel_delta + user_delta
    if idle_delta < 0 or kernel_delta < 0 or user_delta < 0 or total_delta <= 0:
        return None
    busy_delta = max(total_delta - idle_delta, 0)
    return _clamp_percent(100.0 * busy_delta / total_delta)


def parse_luid(instance_name: str) -> LuidKey | None:
    """Extract a DXGI adapter LUID from a Windows performance-counter instance."""

    match = _LUID_RE.search(instance_name)
    if match is None:
        return None
    return (int(match.group("high"), 16), int(match.group("low"), 16))


def aggregate_process_gpu_engine_usage(
    items: tuple[tuple[str, float], ...],
) -> dict[int, float]:
    """Return Task Manager-style per-process GPU usage from GPU Engine counters.

    Multiple counter instances can describe the same physical engine for one process.
    Aggregate duplicates for that engine first, then use the busiest engine as the
    process-level utilization value. This keeps the ranking bounded to 0-100%.
    """

    engines: dict[tuple[int, LuidKey, int, int, str], float] = {}
    for name, raw_value in items:
        match = _PROCESS_ENGINE_RE.search(name)
        if match is None:
            continue
        pid = int(match.group("pid"))
        if pid <= 0:
            continue
        luid = (int(match.group("high"), 16), int(match.group("low"), 16))
        key = (
            pid,
            luid,
            int(match.group("phys")),
            int(match.group("engine")),
            match.group("type").casefold(),
        )
        engines[key] = engines.get(key, 0.0) + max(raw_value, 0.0)

    by_process: dict[int, float] = {}
    for (pid, _luid, _phys, _engine, _type), value in engines.items():
        by_process[pid] = max(by_process.get(pid, 0.0), _clamp_percent(value))
    return by_process


def sample_process_gpu_usage(
    *,
    sample_interval_seconds: float = 0.25,
) -> dict[int, float]:
    """Sample per-process Windows GPU activity for FPS target ranking.

    PDH rate counters need a priming collection and a later collection. The sleep runs
    on the FPS broker worker, never on the Qt UI thread. Missing counters degrade to an
    empty ranking so foreground/z-order targeting remains available.
    """

    if sample_interval_seconds < 0:
        raise ValueError("sample_interval_seconds must not be negative")
    if sys.platform != "win32":
        return {}
    query = _PdhGpuQuery()
    try:
        if sample_interval_seconds:
            time.sleep(sample_interval_seconds)
        engine_items, _memory_items = query.collect()
        return aggregate_process_gpu_engine_usage(engine_items)
    finally:
        query.close()


def aggregate_gpu_engine_usage(
    items: tuple[tuple[str, float], ...],
) -> dict[LuidKey, float]:
    """Aggregate process counters per physical engine, then take Task Manager-style max."""

    engines: dict[tuple[LuidKey, int, int, str], float] = {}
    for name, raw_value in items:
        match = _ENGINE_RE.search(name)
        if match is None:
            continue
        luid = (int(match.group("high"), 16), int(match.group("low"), 16))
        key = (
            luid,
            int(match.group("phys")),
            int(match.group("engine")),
            match.group("type").casefold(),
        )
        engines[key] = engines.get(key, 0.0) + max(raw_value, 0.0)

    by_adapter: dict[LuidKey, float] = {}
    for (luid, _phys, _engine, _type), value in engines.items():
        by_adapter[luid] = max(by_adapter.get(luid, 0.0), _clamp_percent(value))
    return by_adapter


def aggregate_adapter_memory(
    items: tuple[tuple[str, float], ...],
) -> dict[LuidKey, int]:
    """Sum dedicated-memory counter values by adapter LUID."""

    result: dict[LuidKey, int] = {}
    for name, raw_value in items:
        luid = parse_luid(name)
        if luid is None:
            continue
        result[luid] = result.get(luid, 0) + max(round(raw_value), 0)
    return result


def select_gpu_adapter(
    adapters: tuple[GpuAdapterInfo, ...],
    utilization: dict[LuidKey, float],
    memory_usage: dict[LuidKey, int],
) -> GpuAdapterInfo | None:
    """Select the most active adapter with deterministic memory/name tie-breakers."""

    if not adapters:
        return None
    return max(
        adapters,
        key=lambda adapter: (
            utilization.get(adapter.luid, -1.0),
            memory_usage.get(adapter.luid, -1),
            adapter.dedicated_bytes,
            adapter.name.casefold(),
        ),
    )


def _enumerate_dxgi_adapters() -> tuple[GpuAdapterInfo, ...]:
    dxgi = _load_windll("dxgi")
    create_factory = dxgi.CreateDXGIFactory1
    create_factory.argtypes = (ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
    create_factory.restype = ctypes.c_long
    factory = ctypes.c_void_p()
    iid = _guid("770AAE78-F26F-4DBA-A829-253C83D1B387")
    hr = int(create_factory(ctypes.byref(iid), ctypes.byref(factory)))
    if hr < 0 or not factory.value:
        _LOGGER.debug("CreateDXGIFactory1 failed: 0x%08X", hr & 0xFFFFFFFF)
        return ()

    result: list[GpuAdapterInfo] = []
    try:
        enum_adapters = _com_function(
            factory,
            12,
            ctypes.c_long,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
        )
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            hr = int(enum_adapters(factory, index, ctypes.byref(adapter)))
            if hr & 0xFFFFFFFF == _DXGI_ERROR_NOT_FOUND:
                break
            if hr < 0 or not adapter.value:
                break
            try:
                desc = _DXGI_ADAPTER_DESC1()
                get_desc = _com_function(
                    adapter,
                    10,
                    ctypes.c_long,
                    ctypes.POINTER(_DXGI_ADAPTER_DESC1),
                )
                desc_hr = int(get_desc(adapter, ctypes.byref(desc)))
                if desc_hr >= 0:
                    luid = (
                        int(desc.AdapterLuid.HighPart) & 0xFFFFFFFF,
                        int(desc.AdapterLuid.LowPart) & 0xFFFFFFFF,
                    )
                    result.append(
                        GpuAdapterInfo(
                            luid=luid,
                            name=str(desc.Description).rstrip("\x00").strip(),
                            dedicated_bytes=int(desc.DedicatedVideoMemory),
                        )
                    )
            finally:
                _release_com(adapter)
            index += 1
    finally:
        _release_com(factory)
    return tuple(result)


def _guid(value: str) -> _GUID:
    import uuid

    parsed = uuid.UUID(value)
    raw = parsed.bytes_le
    data4 = (ctypes.c_ubyte * 8).from_buffer_copy(raw[8:])
    return _GUID(
        int.from_bytes(raw[0:4], "little"),
        int.from_bytes(raw[4:6], "little"),
        int.from_bytes(raw[6:8], "little"),
        data4,
    )


def _load_windll(name: str) -> Any:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError(f"Windows DLL loader is unavailable for {name}")
    return loader(name, use_last_error=True)


def _win_error() -> OSError:
    get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
    win_error = getattr(ctypes, "WinError", None)
    code = int(get_last_error())
    if win_error is None:
        return OSError(code, "Windows API call failed")
    return cast(OSError, win_error(code))


def _com_function(
    pointer: ctypes.c_void_p,
    index: int,
    restype: Any,
    *argtypes: Any,
) -> Any:
    vtable = ctypes.cast(
        pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    ).contents
    address = vtable[index]
    factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    function_type = factory(restype, ctypes.c_void_p, *argtypes)
    return function_type(address)


def _release_com(pointer: ctypes.c_void_p) -> None:
    if not pointer.value:
        return
    release = _com_function(pointer, 2, wintypes.ULONG)
    release(pointer)


def _filetime_value(value: _FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _percentage(used: int | None, total: int | None) -> float | None:
    if used is None or total is None or total <= 0:
        return None
    return _clamp_percent(100.0 * used / total)


def _clamp_percent(value: float) -> float:
    return min(max(value, 0.0), 100.0)
