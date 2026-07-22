"""Stage a compiled Playnite bridge into Vigil package data with a hash manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_ROOT = PROJECT_ROOT / "integrations" / "playnite" / "VigilOverlayBridge"
DEFAULT_DLL = BRIDGE_ROOT / "bin" / "Release" / "net462" / "VigilOverlayBridge.dll"
EXTENSION_MANIFEST = BRIDGE_ROOT / "extension.yaml"
DESTINATION = (
    PROJECT_ROOT / "src" / "vigil_overlay" / "resources" / "integrations" / "playnite"
)
EXTENSION_ID = "7c04ef12-67ae-4db7-ae4f-3af7fb227809"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    return values


def stage(dll_path: Path = DEFAULT_DLL, destination: Path = DESTINATION) -> Path:
    dll = dll_path.expanduser().resolve()
    if not dll.is_file():
        raise FileNotFoundError(f"Compiled Playnite bridge DLL was not found: {dll}")
    if not EXTENSION_MANIFEST.is_file():
        raise FileNotFoundError(
            f"Playnite extension manifest was not found: {EXTENSION_MANIFEST}"
        )

    values = _manifest_values(EXTENSION_MANIFEST)
    if values.get("Id") != EXTENSION_ID:
        raise ValueError(
            "Playnite bridge extension ID does not match the trusted Vigil ID"
        )
    if values.get("Module") != "VigilOverlayBridge.dll":
        raise ValueError(
            "Playnite bridge manifest Module must be VigilOverlayBridge.dll"
        )
    if values.get("Type") != "GenericPlugin":
        raise ValueError("Playnite bridge manifest Type must be GenericPlugin")
    version = values.get("Version")
    if not version:
        raise ValueError("Playnite bridge manifest Version is missing")

    destination.mkdir(parents=True, exist_ok=True)
    staged_dll = destination / "VigilOverlayBridge.dll"
    staged_manifest = destination / "extension.yaml"
    shutil.copy2(dll, staged_dll)
    shutil.copy2(EXTENSION_MANIFEST, staged_manifest)
    payload = {
        "dll_filename": staged_dll.name,
        "dll_sha256": _sha256(staged_dll),
        "extension_id": EXTENSION_ID,
        "version": version,
    }
    manifest_path = destination / "bridge_manifest.json"
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dll", type=Path, default=DEFAULT_DLL)
    parser.add_argument("--destination", type=Path, default=DESTINATION)
    args = parser.parse_args()
    try:
        manifest = stage(args.dll, args.destination)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(str(exc))
        return 2
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
