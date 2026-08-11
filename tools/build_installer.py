"""Build the Vigil Overlay Windows installer with trusted prerequisites embedded."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from vigil_overlay.core.file_io import sha256_file
from vigil_overlay.core.packaging import validate_playnite_bundle
from vigil_overlay.core.version import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]
Nuitka_OUTPUT_ROOT = PROJECT_ROOT / "build" / "nuitka"
INSTALLER_ROOT = PROJECT_ROOT / "installer"
INSTALLER_SCRIPT = INSTALLER_ROOT / "VigilOverlay.iss"
DEFAULT_GAMEINPUT_MSI = (
    PROJECT_ROOT / "vendor" / "Microsoft.GameInput" / "redist" / "GameInputRedist.msi"
)
HIDHIDE_VERSION = "1.5.230"
HIDHIDE_FILENAME = f"HidHide_{HIDHIDE_VERSION}_x64.exe"
HIDHIDE_SHA256 = "f4bbbcb82e6258641b887c74bc81c4c5f66e4aa811808dfc304347687b7605f6"
DEFAULT_HIDHIDE_INSTALLER = (
    PROJECT_ROOT / "vendor" / "Nefarius.HidHide" / "redist" / HIDHIDE_FILENAME
)
OUTPUT_ROOT = PROJECT_ROOT / "build" / "installer"
PRESENTMON_FILENAME = "PresentMon-2.5.1-x64.exe"
PRESENTMON_SHA256 = "9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191"
PRESENTMON_LEGAL_FILENAMES = ("LICENSE.txt", "NOTICE.txt", "THIRD_PARTY.txt")
REQUIRED_LEGAL_DISTRIBUTION_FILES = (
    Path("LICENSE.txt"),
    Path("NOTICE.txt"),
    Path("THIRD_PARTY_NOTICES.md"),
    Path("TRADEMARKS.md"),
    Path("licenses/third_party/Python/LICENSE.txt"),
    Path("licenses/third_party/Qt_for_Python/LGPL-3.0.txt"),
    Path("licenses/third_party/Qt_for_Python/GPL-3.0.txt"),
    Path("licenses/third_party/Microsoft.GameInput/LICENSE.txt"),
    Path("licenses/third_party/Nefarius.HidHide/LICENSE.txt"),
)
_MICROSOFT_PUBLISHER = "microsoft corporation"
_NEFARIUS_PUBLISHER = "nefarius software solutions e.u."
_GAMEINPUT_PRODUCT_NAMES = {
    "gameinput redistributable",
    "microsoft gameinput",
    "microsoft gameinput redistributable",
}
_PRODUCT_CODE = re.compile(
    r"^\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}$",
    re.IGNORECASE,
)
_PRODUCT_VERSION = re.compile(r"^\d+(?:\.\d+){1,3}$")


@dataclass(frozen=True, slots=True)
class GameInputMsiIdentity:
    """Cryptographic and MSI-database identity for an approved prerequisite."""

    path: Path
    signature_status: str
    signer_subject: str
    signer_thumbprint: str
    product_name: str
    manufacturer: str
    product_code: str
    product_version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class HidHideInstallerIdentity:
    """Authenticode and file-version identity for the approved HidHide setup."""

    path: Path
    signature_status: str
    signer_subject: str
    signer_thumbprint: str
    product_name: str
    company_name: str
    file_version: str
    product_version: str
    sha256: str


def resolve_nuitka_dist(root: Path = Nuitka_OUTPUT_ROOT) -> Path:
    candidates = sorted(
        path.parent for path in root.glob("*.dist/VigilOverlay.exe") if path.is_file()
    )
    if len(candidates) != 1:
        raise FileNotFoundError(
            "Expected exactly one Nuitka standalone directory containing VigilOverlay.exe; "
            f"found {len(candidates)} under {root}"
        )
    return candidates[0]


def resolve_iscc(explicit: Path | None = None) -> Path:
    if explicit is not None:
        candidate = explicit.expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"Inno Setup compiler was not found: {candidate}")

    from_path = shutil.which("ISCC.exe") or shutil.which("iscc")
    if from_path:
        return Path(from_path).resolve()

    roots = [
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
    ]
    for raw_root in roots:
        if not raw_root:
            continue
        candidate = Path(raw_root) / "Inno Setup 6" / "ISCC.exe"
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("Inno Setup 6 compiler (ISCC.exe) is required to build the installer")


def validate_packaged_presentmon(app_source_dir: Path) -> None:
    presentmon = (
        app_source_dir
        / "vigil_overlay"
        / "resources"
        / "third_party"
        / "presentmon"
        / "bin"
        / PRESENTMON_FILENAME
    )
    if not presentmon.is_file():
        raise FileNotFoundError(
            "Installer build refused a Vigil distribution missing the packaged PresentMon "
            f"collector: {presentmon}"
        )
    if sha256_file(presentmon) != PRESENTMON_SHA256:
        raise FileNotFoundError(
            "Installer build refused a Vigil distribution with an untrusted PresentMon collector"
        )
    legal_root = presentmon.parent.parent
    missing_legal = [
        legal_root / filename
        for filename in PRESENTMON_LEGAL_FILENAMES
        if not (legal_root / filename).is_file()
    ]
    if missing_legal:
        raise FileNotFoundError(
            "Installer build refused a Vigil distribution missing PresentMon legal files: "
            + ", ".join(str(path) for path in missing_legal)
        )


def validate_packaged_playnite_bridge(app_source_dir: Path) -> None:
    bridge_root = app_source_dir / "vigil_overlay" / "resources" / "integrations" / "playnite"
    validate_playnite_bundle(bridge_root)


def validate_packaged_legal_materials(app_source_dir: Path) -> None:
    """Refuse installer creation when required legal material is missing."""

    missing = [
        app_source_dir / relative
        for relative in REQUIRED_LEGAL_DISTRIBUTION_FILES
        if not (app_source_dir / relative).is_file()
    ]
    if missing:
        raise FileNotFoundError(
            "Installer build refused a Vigil distribution missing legal material: "
            + ", ".join(str(path) for path in missing)
        )


def inspect_gameinput_msi(path: Path) -> GameInputMsiIdentity:
    """Read Authenticode and MSI properties on the Windows release machine."""

    if os.name != "nt":
        raise ValueError("GameInput MSI trust verification must run on Windows")
    powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if powershell is None:
        raise ValueError("PowerShell is required for GameInput Authenticode verification")
    environment = os.environ.copy()
    environment["VIGIL_GAMEINPUT_MSI"] = str(path)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$msiPath = $env:VIGIL_GAMEINPUT_MSI; "
        "$signature = Get-AuthenticodeSignature -LiteralPath $msiPath; "
        "if ($null -eq $signature) { throw 'Signature inspection returned no result' }; "
        "$subject = if ($null -eq $signature.SignerCertificate) { '' } else { "
        "$signature.SignerCertificate.Subject }; "
        "$thumbprint = if ($null -eq $signature.SignerCertificate) { '' } else { "
        "$signature.SignerCertificate.Thumbprint }; "
        "$installer = New-Object -ComObject WindowsInstaller.Installer; "
        "$database = $installer.GetType().InvokeMember("
        "'OpenDatabase', 'InvokeMethod', $null, $installer, @($msiPath, 0)); "
        "$properties = [ordered]@{}; "
        "foreach ($name in @('ProductName','Manufacturer','ProductCode','ProductVersion')) { "
        "$query = \"SELECT ``Value`` FROM ``Property`` WHERE ``Property``='$name'\"; "
        "$view = $database.GetType().InvokeMember("
        "'OpenView', 'InvokeMethod', $null, $database, @($query)); "
        "$view.GetType().InvokeMember("
        "'Execute', 'InvokeMethod', $null, $view, $null) | Out-Null; "
        "$record = $view.GetType().InvokeMember("
        "'Fetch', 'InvokeMethod', $null, $view, $null); "
        'if ($null -eq $record) { throw "MSI property is missing: $name" }; '
        "$value = $record.GetType().InvokeMember("
        "'StringData', 'GetProperty', $null, $record, 1); "
        "if ([string]::IsNullOrWhiteSpace([string]$value)) { "
        'throw "MSI property is empty: $name" }; '
        "$properties[$name] = [string]$value; "
        "}; "
        "[PSCustomObject]@{ status = [string]$signature.Status; subject = $subject; "
        "thumbprint = $thumbprint; product_name = $properties['ProductName']; "
        "manufacturer = $properties['Manufacturer']; "
        "product_code = $properties['ProductCode']; "
        "product_version = $properties['ProductVersion'] } | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise ValueError(
            "Could not inspect the GameInput MSI trust and product metadata: "
            + (result.stderr.strip() or f"PowerShell exit code {result.returncode}")
        )
    try:
        inspection: object = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("GameInput MSI inspection returned invalid data") from exc
    if not isinstance(inspection, dict):
        raise ValueError("GameInput MSI inspection returned an invalid object")

    return GameInputMsiIdentity(
        path=path,
        signature_status=str(inspection.get("status", "")).strip(),
        signer_subject=str(inspection.get("subject", "")).strip(),
        signer_thumbprint=str(inspection.get("thumbprint", "")).strip(),
        product_name=_required_inspection_text(inspection, "product_name"),
        manufacturer=_required_inspection_text(inspection, "manufacturer"),
        product_code=_required_inspection_text(inspection, "product_code"),
        product_version=_required_inspection_text(inspection, "product_version"),
        sha256=sha256_file(path),
    )


def _required_inspection_text(payload: dict[object, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"GameInput MSI inspection returned an empty field: {name}")
    value = value.strip()
    return value


def validate_gameinput_msi(
    path: Path,
    *,
    inspector: Callable[[Path], GameInputMsiIdentity] | None = None,
    approved_sha256: str | None = None,
) -> GameInputMsiIdentity:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            "Official Microsoft GameInputRedist.msi is required. Stage it at "
            f"{DEFAULT_GAMEINPUT_MSI} or pass --gameinput-msi."
        )
    if resolved.name.casefold() != "gameinputredist.msi":
        raise ValueError("GameInput prerequisite must be named GameInputRedist.msi")
    if resolved.stat().st_size < 32 * 1024:
        raise ValueError("GameInputRedist.msi is unexpectedly small")
    identity = (inspector or inspect_gameinput_msi)(resolved)
    if identity.path.resolve() != resolved:
        raise ValueError("GameInput MSI inspector returned a different file identity")
    if identity.signature_status.casefold() != "valid":
        raise ValueError("GameInputRedist.msi does not have a valid Authenticode signature")
    if _MICROSOFT_PUBLISHER not in identity.signer_subject.casefold():
        raise ValueError("GameInputRedist.msi is not signed by Microsoft Corporation")
    if not identity.signer_thumbprint.strip():
        raise ValueError("GameInputRedist.msi signer certificate has no thumbprint")
    if identity.product_name.casefold() not in _GAMEINPUT_PRODUCT_NAMES:
        raise ValueError(f"Unexpected GameInput MSI product name: {identity.product_name}")
    if _MICROSOFT_PUBLISHER not in identity.manufacturer.casefold():
        raise ValueError(f"Unexpected GameInput MSI manufacturer: {identity.manufacturer}")
    if not _PRODUCT_CODE.fullmatch(identity.product_code):
        raise ValueError("GameInput MSI ProductCode is not a valid GUID")
    if not _PRODUCT_VERSION.fullmatch(identity.product_version):
        raise ValueError("GameInput MSI ProductVersion is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", identity.sha256, re.IGNORECASE):
        raise ValueError("GameInput MSI SHA-256 identity is invalid")
    if approved_sha256 is not None and identity.sha256.casefold() != approved_sha256.casefold():
        raise ValueError("GameInputRedist.msi does not match the release-approved SHA-256")
    return identity


def inspect_hidhide_installer(path: Path) -> HidHideInstallerIdentity:
    """Read Authenticode and version-resource identity on the release machine."""

    if os.name != "nt":
        raise ValueError("HidHide installer trust verification must run on Windows")
    powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if powershell is None:
        raise ValueError("PowerShell is required for HidHide Authenticode verification")
    environment = os.environ.copy()
    environment["VIGIL_HIDHIDE_INSTALLER"] = str(path)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$installerPath = $env:VIGIL_HIDHIDE_INSTALLER; "
        "$file = Get-Item -LiteralPath $installerPath; "
        "$signature = Get-AuthenticodeSignature -LiteralPath $installerPath; "
        "if ($null -eq $signature) { throw 'Signature inspection returned no result' }; "
        "$subject = if ($null -eq $signature.SignerCertificate) { '' } else { "
        "$signature.SignerCertificate.Subject }; "
        "$thumbprint = if ($null -eq $signature.SignerCertificate) { '' } else { "
        "$signature.SignerCertificate.Thumbprint }; "
        "[PSCustomObject]@{ status = [string]$signature.Status; subject = $subject; "
        "thumbprint = $thumbprint; product_name = [string]$file.VersionInfo.ProductName; "
        "company_name = [string]$file.VersionInfo.CompanyName; "
        "file_version = [string]$file.VersionInfo.FileVersion; "
        "product_version = [string]$file.VersionInfo.ProductVersion } | "
        "ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise ValueError(
            "Could not inspect the HidHide installer trust and product metadata: "
            + (result.stderr.strip() or f"PowerShell exit code {result.returncode}")
        )
    try:
        inspection: object = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("HidHide installer inspection returned invalid data") from exc
    if not isinstance(inspection, dict):
        raise ValueError("HidHide installer inspection returned an invalid object")

    def required(name: str) -> str:
        value = inspection.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"HidHide installer inspection returned an empty field: {name}")
        return value.strip()

    return HidHideInstallerIdentity(
        path=path,
        signature_status=str(inspection.get("status", "")).strip(),
        signer_subject=str(inspection.get("subject", "")).strip(),
        signer_thumbprint=str(inspection.get("thumbprint", "")).strip(),
        product_name=required("product_name"),
        company_name=required("company_name"),
        file_version=required("file_version"),
        product_version=required("product_version"),
        sha256=sha256_file(path),
    )


def validate_hidhide_installer(
    path: Path,
    *,
    inspector: Callable[[Path], HidHideInstallerIdentity] | None = None,
) -> HidHideInstallerIdentity:
    """Require the exact approved official HidHide release asset."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            "Official HidHide installer is required. Stage it at "
            f"{DEFAULT_HIDHIDE_INSTALLER} or pass --hidhide-installer."
        )
    if resolved.name.casefold() != HIDHIDE_FILENAME.casefold():
        raise ValueError(f"HidHide prerequisite must be named {HIDHIDE_FILENAME}")
    if resolved.stat().st_size < 1024 * 1024:
        raise ValueError(f"{HIDHIDE_FILENAME} is unexpectedly small")
    identity = (inspector or inspect_hidhide_installer)(resolved)
    if identity.path.resolve() != resolved:
        raise ValueError("HidHide installer inspector returned a different file identity")
    if identity.signature_status.casefold() != "valid":
        raise ValueError("HidHide installer does not have a valid Authenticode signature")
    if _NEFARIUS_PUBLISHER not in identity.signer_subject.casefold():
        raise ValueError("HidHide installer is not signed by Nefarius Software Solutions")
    if not identity.signer_thumbprint.strip():
        raise ValueError("HidHide installer signer certificate has no thumbprint")
    if identity.product_name.casefold() != "hidhide":
        raise ValueError(f"Unexpected HidHide product name: {identity.product_name}")
    if _NEFARIUS_PUBLISHER not in identity.company_name.casefold():
        raise ValueError(f"Unexpected HidHide company name: {identity.company_name}")
    if identity.file_version != HIDHIDE_VERSION:
        raise ValueError(f"Unexpected HidHide file version: {identity.file_version}")
    if identity.product_version != HIDHIDE_VERSION:
        raise ValueError(f"Unexpected HidHide product version: {identity.product_version}")
    if identity.sha256.casefold() != HIDHIDE_SHA256:
        raise ValueError("HidHide installer does not match the release-approved SHA-256")
    return identity


def build_command(
    *,
    iscc: Path,
    app_source_dir: Path,
    gameinput_msi: Path,
    hidhide_installer: Path,
    output_dir: Path,
) -> list[str]:
    return [
        str(iscc),
        f"/DAppSourceDir={app_source_dir}",
        f"/DGameInputMsi={gameinput_msi}",
        f"/DHidHideInstaller={hidhide_installer}",
        f"/DMyAppVersion={__version__}",
        f"/O{output_dir}",
        str(INSTALLER_SCRIPT),
    ]


def write_prerequisite_manifest(gameinput: GameInputMsiIdentity, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "name": "Microsoft GameInput Redistributable",
        "filename": gameinput.path.name,
        "identity": {key: value for key, value in asdict(gameinput).items() if key != "path"},
        "source_package": "Microsoft.GameInput",
        "install_mode": "bundled-msi-before-vigil",
    }
    destination = output_dir / "gameinput_prerequisite.json"
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def write_hidhide_prerequisite_manifest(
    hidhide: HidHideInstallerIdentity, output_dir: Path
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "name": "HidHide",
        "filename": hidhide.path.name,
        "identity": {key: value for key, value in asdict(hidhide).items() if key != "path"},
        "source_release": "nefarius/HidHide v1.5.230.0",
        "install_mode": "bundled-optional-exe-when-missing",
        "selection_default": "selected-by-default-when-missing",
        "configuration_mode": "vigil-first-run-fresh-install-only",
        "uninstall_mode": "shared-dependency-left-installed",
    }
    destination = output_dir / "hidhide_prerequisite.json"
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gameinput-msi", type=Path, default=DEFAULT_GAMEINPUT_MSI)
    parser.add_argument("--hidhide-installer", type=Path, default=DEFAULT_HIDHIDE_INSTALLER)
    parser.add_argument("--nuitka-dist", type=Path)
    parser.add_argument("--iscc", type=Path)
    parser.add_argument(
        "--approved-gameinput-sha256",
        help="Optional release-pipeline SHA-256 pin in addition to signature and MSI identity.",
    )
    parser.add_argument("--print-only", action="store_true")
    args = parser.parse_args(argv)

    try:
        gameinput = validate_gameinput_msi(
            args.gameinput_msi,
            approved_sha256=args.approved_gameinput_sha256,
        )
        hidhide = validate_hidhide_installer(args.hidhide_installer)
        app_source_dir = (
            args.nuitka_dist.expanduser().resolve()
            if args.nuitka_dist is not None
            else resolve_nuitka_dist()
        )
        if not (app_source_dir / "VigilOverlay.exe").is_file():
            raise FileNotFoundError(
                f"Nuitka standalone directory does not contain VigilOverlay.exe: {app_source_dir}"
            )
        validate_packaged_presentmon(app_source_dir)
        validate_packaged_playnite_bridge(app_source_dir)
        validate_packaged_legal_materials(app_source_dir)
        iscc = resolve_iscc(args.iscc)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    command = build_command(
        iscc=iscc,
        app_source_dir=app_source_dir,
        gameinput_msi=gameinput.path,
        hidhide_installer=hidhide.path,
        output_dir=OUTPUT_ROOT,
    )
    print(subprocess.list2cmdline(command))
    if args.print_only:
        return 0
    if os.name != "nt":
        print(
            "Vigil Overlay installer builds must be executed on Windows.",
            file=sys.stderr,
        )
        return 2

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    write_prerequisite_manifest(gameinput, OUTPUT_ROOT)
    write_hidhide_prerequisite_manifest(hidhide, OUTPUT_ROOT)
    return subprocess.call(command, cwd=PROJECT_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
