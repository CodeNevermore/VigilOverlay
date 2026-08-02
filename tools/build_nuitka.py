"""Standalone-first Nuitka build command generator and runner."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from vigil_overlay.core.file_io import sha256_file
from vigil_overlay.core.packaging import validate_playnite_bundle
from vigil_overlay.core.version import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ENTRY = PROJECT_ROOT / "src" / "vigil_overlay"
OUTPUT_ROOT = PROJECT_ROOT / "build" / "nuitka"
APPLICATION_ICON_RESOURCE = (
    PROJECT_ROOT / "src" / "vigil_overlay" / "resources" / "icons" / "vigil_overlay.ico"
)
APPLICATION_ICON_SHA256 = (
    "78c2473d40dad9c4629f1208022a1e3b45ae85b3e6b10e6da98b9b75df4bcaf6"
)
PRESENTMON_FILENAME = "PresentMon-2.5.1-x64.exe"
PRESENTMON_SHA256 = "9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191"
PRESENTMON_RELEASE_URL = f"https://github.com/GameTechDev/PresentMon/releases/download/v2.5.1/{PRESENTMON_FILENAME}"
PRESENTMON_MAX_BYTES = 2 * 1024 * 1024
PRESENTMON_RESOURCE_EXE = (
    PROJECT_ROOT
    / "src"
    / "vigil_overlay"
    / "resources"
    / "third_party"
    / "presentmon"
    / "bin"
    / PRESENTMON_FILENAME
)
PRESENTMON_LEGAL_ROOT = PRESENTMON_RESOURCE_EXE.parent.parent
PRESENTMON_LEGAL_FILES = tuple(
    PRESENTMON_LEGAL_ROOT / filename
    for filename in ("LICENSE.txt", "NOTICE.txt", "THIRD_PARTY.txt")
)
THIRD_PARTY_LICENSE_ROOT = PROJECT_ROOT / "third_party_licenses"
LEGAL_DISTRIBUTION_FILES = (
    (PROJECT_ROOT / "LICENSE", "LICENSE.txt"),
    (PROJECT_ROOT / "NOTICE", "NOTICE.txt"),
    (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"),
    (PROJECT_ROOT / "TRADEMARKS.md", "TRADEMARKS.md"),
)
REQUIRED_THIRD_PARTY_LICENSE_FILES = (
    Path("Python/LICENSE.txt"),
    Path("Qt_for_Python/LGPL-3.0.txt"),
    Path("Qt_for_Python/GPL-3.0.txt"),
    Path("Qt_for_Python/NOTICE.txt"),
    Path("pycaw/LICENSE.txt"),
    Path("comtypes/LICENSE.txt"),
    Path("psutil/LICENSE.txt"),
    Path("PyWinRT/LICENSE.txt"),
    Path("Microsoft.GameInput/LICENSE.txt"),
    Path("PlayniteSDK/LICENSE.txt"),
    Path("Inno_Setup/LICENSE.txt"),
)
PLAYNITE_BRIDGE_ROOT = PROJECT_ROOT / "integrations" / "playnite" / "VigilOverlayBridge"
PLAYNITE_BRIDGE_PROJECT = PLAYNITE_BRIDGE_ROOT / "VigilOverlayBridge.csproj"
PLAYNITE_BRIDGE_DLL = (
    PLAYNITE_BRIDGE_ROOT / "bin" / "Release" / "net462" / "VigilOverlayBridge.dll"
)
PLAYNITE_RESOURCE_ROOT = (
    PROJECT_ROOT / "src" / "vigil_overlay" / "resources" / "integrations" / "playnite"
)
PLAYNITE_RESOURCE_DLL = PLAYNITE_RESOURCE_ROOT / "VigilOverlayBridge.dll"
PLAYNITE_RESOURCE_EXTENSION = PLAYNITE_RESOURCE_ROOT / "extension.yaml"
PLAYNITE_RESOURCE_MANIFEST = PLAYNITE_RESOURCE_ROOT / "bridge_manifest.json"


@dataclass(frozen=True, slots=True)
class WinRTProjection:
    import_name: str
    distribution_name: str
    version: str = "3.2.1"


WINRT_PROJECTIONS = (
    WinRTProjection("winrt.windows.foundation", "winrt-Windows.Foundation"),
    WinRTProjection(
        "winrt.windows.foundation.collections",
        "winrt-Windows.Foundation.Collections",
    ),
    WinRTProjection("winrt.windows.gaming.input", "winrt-Windows.Gaming.Input"),
    WinRTProjection("winrt.windows.networking", "winrt-Windows.Networking"),
    WinRTProjection(
        "winrt.windows.networking.connectivity",
        "winrt-Windows.Networking.Connectivity",
    ),
    WinRTProjection("winrt.windows.storage.streams", "winrt-Windows.Storage.Streams"),
)
WINRT_RUNTIME_PACKAGE = "winrt.runtime"
WINRT_RUNTIME_MODULE = "winrt.system"
WINRT_PROJECTION_PACKAGES = tuple(item.import_name for item in WINRT_PROJECTIONS)
WINRT_REQUIRED_IMPORTS = (
    WINRT_RUNTIME_PACKAGE,
    WINRT_RUNTIME_MODULE,
    *WINRT_PROJECTION_PACKAGES,
)


def build_command(profile: str) -> list[str]:
    report_path = OUTPUT_ROOT / f"host-{profile}-compilation-report.xml"
    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--python-flag=-m",
        "--assume-yes-for-downloads",
        "--output-filename=VigilOverlay.exe",
        "--product-name=Vigil Overlay",
        f"--file-version={__version__}",
        f"--product-version={__version__}",
        "--file-description=Vigil Overlay",
        f"--windows-icon-from-ico={APPLICATION_ICON_RESOURCE}",
        f"--output-dir={OUTPUT_ROOT}",
        f"--report={report_path}",
        "--include-package=pycaw",
        "--include-module=comtypes",
        "--include-module=comtypes.automation",
        "--nofollow-import-to=comtypes.test.*",
        "--include-package=psutil",
        f"--include-package={WINRT_RUNTIME_PACKAGE}",
        f"--include-module={WINRT_RUNTIME_MODULE}",
        *(f"--include-package={package}" for package in WINRT_PROJECTION_PACKAGES),
        "--include-package-data=vigil_overlay",
        (
            f"--include-data-dir={PROJECT_ROOT / 'src' / 'vigil_overlay' / 'resources'}="
            "vigil_overlay/resources"
        ),
        (
            f"--include-data-files={PRESENTMON_RESOURCE_EXE}="
            "vigil_overlay/resources/third_party/presentmon/bin/"
            f"{PRESENTMON_FILENAME}"
        ),
        *(
            f"--include-data-files={source}={destination}"
            for source, destination in LEGAL_DISTRIBUTION_FILES
        ),
        (f"--include-data-dir={THIRD_PARTY_LICENSE_ROOT}=" "licenses/third_party"),
        "--enable-plugin=pyside6",
    ]

    if profile == "diagnostic":
        command.append("--windows-console-mode=force")
    elif profile == "production":
        command.extend(
            [
                "--windows-console-mode=disable",
                (
                    f"--include-data-files={PLAYNITE_RESOURCE_DLL}="
                    "vigil_overlay/resources/integrations/playnite/VigilOverlayBridge.dll"
                ),
                (
                    f"--include-data-files={PLAYNITE_RESOURCE_EXTENSION}="
                    "vigil_overlay/resources/integrations/playnite/extension.yaml"
                ),
                (
                    f"--include-data-files={PLAYNITE_RESOURCE_MANIFEST}="
                    "vigil_overlay/resources/integrations/playnite/bridge_manifest.json"
                ),
            ]
        )
    else:
        raise ValueError(f"Unsupported profile: {profile}")

    command.append(str(PACKAGE_ENTRY))
    return command


def validate_winrt_build_dependencies() -> None:
    """Fail before Nuitka when a required Windows Runtime projection is unavailable."""

    failures: list[tuple[str, Exception]] = []
    for module_name in WINRT_REQUIRED_IMPORTS:
        try:
            importlib.import_module(module_name)
        except (ImportError, OSError) as exc:
            failures.append((module_name, exc))

    if not failures:
        return

    details = "; ".join(
        f"{module_name}: {type(exc).__name__}: {exc}" for module_name, exc in failures
    )
    raise FileNotFoundError(
        "Vigil's Windows build environment is missing or cannot load required WinRT "
        f"projections ({details}). Reinstall the finalized build dependencies with "
        'python -m pip install -e ".[build]" using the same Python interpreter that '
        "will run Nuitka."
    ) from failures[0][1]


def validate_winrt_dependency_contract(
    project_file: Path = PROJECT_ROOT / "pyproject.toml",
    projections: Sequence[WinRTProjection] = WINRT_PROJECTIONS,
) -> None:
    """Require exact agreement between build imports and declared Windows projections."""

    try:
        project = tomllib.loads(project_file.read_text(encoding="utf-8"))
        dependencies = project["project"]["dependencies"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise FileNotFoundError(
            f"Could not read the project dependency contract: {exc}"
        ) from exc
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        raise FileNotFoundError("Project dependencies must be an array of strings")

    declared: dict[str, tuple[str, str]] = {}
    for dependency in dependencies:
        requirement, separator, marker = dependency.partition(";")
        name, pin, version = requirement.strip().partition("==")
        normalized = _normalize_distribution_name(name)
        if not normalized.startswith("winrt-windows-"):
            continue
        if pin != "==":
            raise FileNotFoundError(
                f"WinRT dependency must use an exact version pin: {name}"
            )
        declared[normalized] = (version.strip(), marker.strip() if separator else "")

    expected = {
        _normalize_distribution_name(item.distribution_name): item
        for item in projections
    }
    if set(declared) != set(expected):
        missing = sorted(set(expected) - set(declared))
        unexpected = sorted(set(declared) - set(expected))
        raise FileNotFoundError(
            "WinRT dependency/include contract drifted "
            f"(missing={missing}, unexpected={unexpected})"
        )
    for normalized, projection in expected.items():
        version, marker = declared[normalized]
        if version != projection.version:
            raise FileNotFoundError(
                f"{projection.distribution_name} must be pinned to {projection.version}"
            )
        if marker not in {"sys_platform == 'win32'", 'sys_platform == "win32"'}:
            raise FileNotFoundError(
                f"{projection.distribution_name} must remain Windows-only"
            )


def _normalize_distribution_name(name: str) -> str:
    normalized = name.strip().casefold()
    for separator in (".", "_"):
        normalized = normalized.replace(separator, "-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized


def validate_application_icon() -> None:
    """Fail builds that cannot embed Vigil's canonical multi-size Windows icon."""

    if not APPLICATION_ICON_RESOURCE.is_file():
        raise FileNotFoundError(
            f"Build requires the canonical Vigil application icon: {APPLICATION_ICON_RESOURCE}"
        )
    payload = APPLICATION_ICON_RESOURCE.read_bytes()
    if len(payload) < 6 or payload[:4] != b"\x00\x00\x01\x00":
        raise FileNotFoundError(
            "Vigil application icon is not a valid Windows ICO resource"
        )
    image_count = int.from_bytes(payload[4:6], "little")
    if image_count < 1 or len(payload) < 6 + image_count * 16:
        raise FileNotFoundError("Vigil application icon directory is truncated")
    if sha256_file(APPLICATION_ICON_RESOURCE) != APPLICATION_ICON_SHA256:
        raise FileNotFoundError(
            "Vigil application icon does not match the approved canonical asset"
        )


def validate_packaged_application_icon(dist_root: Path) -> None:
    """Fail when the standalone distribution omitted or changed the tray icon asset."""

    packaged = dist_root / "vigil_overlay" / "resources" / "icons" / "vigil_overlay.ico"
    if not packaged.is_file():
        raise FileNotFoundError(
            f"Completed Nuitka distribution is missing the Vigil application icon: {packaged}"
        )
    if sha256_file(packaged) != APPLICATION_ICON_SHA256:
        raise FileNotFoundError(
            "Completed Nuitka distribution contains an unexpected Vigil application icon"
        )


def validate_bundled_presentmon() -> None:
    """Fail a build that cannot package the trusted PresentMon executable."""

    missing_legal = [path for path in PRESENTMON_LEGAL_FILES if not path.is_file()]
    if missing_legal:
        raise FileNotFoundError(
            "Build requires PresentMon's license and upstream notices beside the "
            f"collector: {', '.join(str(path) for path in missing_legal)}"
        )
    if not PRESENTMON_RESOURCE_EXE.is_file():
        raise FileNotFoundError(
            "Build requires the pinned PresentMon executable at "
            f"{PRESENTMON_RESOURCE_EXE}. Vigil never downloads PresentMon at runtime."
        )
    actual_sha256 = sha256_file(PRESENTMON_RESOURCE_EXE)
    if actual_sha256 != PRESENTMON_SHA256:
        raise FileNotFoundError(
            "Build refused the supplied PresentMon executable because its SHA-256 "
            f"does not match the pinned runtime: {PRESENTMON_RESOURCE_EXE}"
        )


def _download_official_presentmon() -> None:
    """Stage the pinned official collector for release packaging, never application runtime."""

    PRESENTMON_RESOURCE_EXE.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        PRESENTMON_RELEASE_URL,
        headers={"User-Agent": "VigilOverlay-ReleaseBuilder/1"},
    )
    temporary_path: Path | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            tempfile.NamedTemporaryFile(
                mode="wb",
                dir=PRESENTMON_RESOURCE_EXE.parent,
                prefix=f".{PRESENTMON_FILENAME}.",
                suffix=".tmp",
                delete=False,
            ) as handle,
        ):
            temporary_path = Path(handle.name)
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > PRESENTMON_MAX_BYTES:
                    raise FileNotFoundError(
                        "Official PresentMon release asset exceeded the bounded 2 MiB "
                        "release-builder download limit"
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, urllib.error.URLError) as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise FileNotFoundError(
            "Could not acquire the pinned official PresentMon release asset during packaging"
        ) from exc
    except FileNotFoundError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise

    if temporary_path is None or total == 0:
        raise FileNotFoundError("Official PresentMon release asset download was empty")
    if digest.hexdigest() != PRESENTMON_SHA256:
        temporary_path.unlink(missing_ok=True)
        raise FileNotFoundError(
            "Downloaded official PresentMon release asset failed pinned SHA-256 verification"
        )
    os.replace(temporary_path, PRESENTMON_RESOURCE_EXE)


def prepare_bundled_presentmon() -> None:
    """Ensure release packaging has the trusted collector without adding runtime acquisition."""

    if PRESENTMON_RESOURCE_EXE.is_file():
        validate_bundled_presentmon()
        return
    print(
        "Pinned PresentMon collector is not staged; acquiring the official release asset "
        "for this build..."
    )
    _download_official_presentmon()
    validate_bundled_presentmon()


def validate_packaged_presentmon(dist_root: Path) -> None:
    """Fail when the completed standalone distribution omitted or corrupted PresentMon."""

    packaged = (
        dist_root
        / "vigil_overlay"
        / "resources"
        / "third_party"
        / "presentmon"
        / "bin"
        / PRESENTMON_FILENAME
    )
    if not packaged.is_file():
        raise FileNotFoundError(
            "Completed Nuitka distribution is missing the packaged PresentMon collector: "
            f"{packaged}"
        )
    if sha256_file(packaged) != PRESENTMON_SHA256:
        raise FileNotFoundError(
            "Completed Nuitka distribution contains an untrusted PresentMon collector"
        )
    packaged_legal_root = packaged.parent.parent
    for source in PRESENTMON_LEGAL_FILES:
        packaged_notice = packaged_legal_root / source.name
        if not packaged_notice.is_file():
            raise FileNotFoundError(
                "Completed Nuitka distribution is missing a PresentMon legal file: "
                f"{packaged_notice}"
            )
        if sha256_file(packaged_notice) != sha256_file(source):
            raise FileNotFoundError(
                "Completed Nuitka distribution changed a PresentMon legal file: "
                f"{packaged_notice}"
            )


def validate_legal_materials() -> None:
    """Require complete, resolved licensing material before compiling a release."""

    required = [
        *(source for source, _destination in LEGAL_DISTRIBUTION_FILES),
        *(
            THIRD_PARTY_LICENSE_ROOT / path
            for path in REQUIRED_THIRD_PARTY_LICENSE_FILES
        ),
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Release licensing material is incomplete: "
            + ", ".join(str(path) for path in missing)
        )

    placeholders = ("[VERIFY]", "[VERSION]", "[LIST", "[LICENSE CONTACT]")
    for source, _destination in LEGAL_DISTRIBUTION_FILES:
        text = source.read_text(encoding="utf-8")
        if any(placeholder in text for placeholder in placeholders):
            raise FileNotFoundError(
                f"Release licensing material contains an unresolved placeholder: {source}"
            )


def validate_packaged_legal_materials(dist_root: Path) -> None:
    """Require the standalone distribution to preserve all release legal files."""

    for source, destination in LEGAL_DISTRIBUTION_FILES:
        packaged = dist_root / destination
        if not packaged.is_file():
            raise FileNotFoundError(
                f"Completed Nuitka distribution is missing a legal file: {packaged}"
            )
        if sha256_file(packaged) != sha256_file(source):
            raise FileNotFoundError(
                f"Completed Nuitka distribution changed a legal file: {packaged}"
            )

    packaged_licenses = dist_root / "licenses" / "third_party"
    for relative in REQUIRED_THIRD_PARTY_LICENSE_FILES:
        source = THIRD_PARTY_LICENSE_ROOT / relative
        packaged = packaged_licenses / relative
        if not packaged.is_file():
            raise FileNotFoundError(
                "Completed Nuitka distribution is missing a third-party license: "
                f"{packaged}"
            )
        if sha256_file(packaged) != sha256_file(source):
            raise FileNotFoundError(
                "Completed Nuitka distribution changed a third-party license: "
                f"{packaged}"
            )


def _validate_playnite_bundle_root(bridge_root: Path) -> None:
    validate_playnite_bundle(bridge_root)


def build_playnite_bridge_release() -> None:
    """Compile the native Playnite bridge on a Windows release machine when needed."""

    if os.name != "nt":
        raise FileNotFoundError(
            "The Playnite bridge is not staged or compiled. A Windows release machine with "
            ".NET Framework 4.6.2 build support is required to compile it."
        )

    msbuild = shutil.which("msbuild.exe") or shutil.which("msbuild")
    dotnet = shutil.which("dotnet.exe") or shutil.which("dotnet")
    if msbuild:
        command = [
            msbuild,
            str(PLAYNITE_BRIDGE_PROJECT),
            "/restore",
            "/t:Build",
            "/p:Configuration=Release",
        ]
    elif dotnet:
        command = [
            dotnet,
            "build",
            str(PLAYNITE_BRIDGE_PROJECT),
            "--configuration",
            "Release",
        ]
    else:
        raise FileNotFoundError(
            "Production build requires MSBuild or the .NET SDK to compile the bundled "
            "Playnite bridge because no compiled Release DLL was found."
        )

    result = subprocess.call(command, cwd=PROJECT_ROOT)
    if result != 0 or not PLAYNITE_BRIDGE_DLL.is_file():
        raise FileNotFoundError(
            "Automatic Playnite bridge Release compilation failed; production packaging stopped."
        )


def prepare_staged_playnite_bridge() -> None:
    """Guarantee a trusted staged bridge exists before a production build."""

    try:
        validate_staged_playnite_bridge()
        return
    except FileNotFoundError:
        pass

    if not PLAYNITE_BRIDGE_DLL.is_file():
        build_playnite_bridge_release()

    result = subprocess.call(
        [sys.executable, str(PROJECT_ROOT / "tools" / "stage_playnite_bridge.py")],
        cwd=PROJECT_ROOT,
    )
    if result != 0:
        raise FileNotFoundError("Automatic Playnite bridge staging failed")
    validate_staged_playnite_bridge()


def validate_staged_playnite_bridge() -> None:
    """Validate the trusted source-tree bridge bundle used as Nuitka input."""

    _validate_playnite_bundle_root(PLAYNITE_RESOURCE_ROOT)


def validate_packaged_playnite_bridge(dist_root: Path) -> None:
    """Fail when the completed standalone distribution omitted or corrupted the bridge."""

    _validate_playnite_bundle_root(
        dist_root / "vigil_overlay" / "resources" / "integrations" / "playnite"
    )


def resolve_built_nuitka_dist(root: Path = OUTPUT_ROOT) -> Path:
    candidates = sorted(
        path.parent for path in root.glob("*.dist/VigilOverlay.exe") if path.is_file()
    )
    if len(candidates) != 1:
        raise FileNotFoundError(
            "Expected exactly one completed Nuitka standalone directory containing "
            f"VigilOverlay.exe; found {len(candidates)} under {root}"
        )
    return candidates[0]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=("diagnostic", "production"),
        default="diagnostic",
    )
    parser.add_argument("--print-only", action="store_true")
    parser.add_argument(
        "--prepare-presentmon",
        action="store_true",
        help="Stage and verify the pinned official PresentMon collector, then exit.",
    )
    args = parser.parse_args(argv)

    if args.prepare_presentmon:
        try:
            prepare_bundled_presentmon()
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"PresentMon ready: {PRESENTMON_RESOURCE_EXE}")
        return 0

    command = build_command(args.profile)
    print(" ".join(command))
    if args.print_only:
        return 0
    if os.name != "nt":
        print("Vigil Overlay builds must be executed on Windows.", file=sys.stderr)
        return 2

    try:
        validate_winrt_dependency_contract()
        validate_winrt_build_dependencies()
        validate_application_icon()
        validate_legal_materials()
        prepare_bundled_presentmon()
        if args.profile == "production":
            prepare_staged_playnite_bridge()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    result = subprocess.call(command, cwd=PROJECT_ROOT)
    if result != 0:
        return result
    try:
        dist_root = resolve_built_nuitka_dist()
        validate_packaged_application_icon(dist_root)
        validate_packaged_legal_materials(dist_root)
        validate_packaged_presentmon(dist_root)
        if args.profile == "production":
            validate_packaged_playnite_bridge(dist_root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
