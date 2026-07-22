"""Shared integrity contracts for native release artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from vigil_overlay.core.file_io import sha256_file

PLAYNITE_EXTENSION_ID = "7c04ef12-67ae-4db7-ae4f-3af7fb227809"


def read_simple_yaml_values(path: Path) -> dict[str, str]:
    """Read the flat key/value subset used by Playnite extension metadata."""

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    return values


def validate_playnite_bundle(bridge_root: Path) -> None:
    """Validate identity, metadata agreement, and DLL integrity for one bundle."""

    dll_path = bridge_root / "VigilOverlayBridge.dll"
    extension_path = bridge_root / "extension.yaml"
    manifest_path = bridge_root / "bridge_manifest.json"
    required = (dll_path, extension_path, manifest_path)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Playnite bridge bundle is incomplete. Missing: " + ", ".join(missing)
        )

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FileNotFoundError(
            f"Playnite bridge bundle manifest is unreadable or invalid: {manifest_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise FileNotFoundError(
            "Playnite bridge bundle manifest root must be an object"
        )

    expected_fields = {"dll_filename", "dll_sha256", "extension_id", "version"}
    if set(payload) != expected_fields:
        raise FileNotFoundError("Playnite bridge bundle manifest fields are invalid")
    if payload.get("extension_id") != PLAYNITE_EXTENSION_ID:
        raise FileNotFoundError("Playnite bridge bundle extension ID is not trusted")
    if payload.get("dll_filename") != "VigilOverlayBridge.dll":
        raise FileNotFoundError("Playnite bridge bundle DLL filename is invalid")

    extension_values = read_simple_yaml_values(extension_path)
    if extension_values.get("Id") != PLAYNITE_EXTENSION_ID:
        raise FileNotFoundError("Playnite extension.yaml ID is not trusted")
    if extension_values.get("Module") != "VigilOverlayBridge.dll":
        raise FileNotFoundError("Playnite extension.yaml module is invalid")
    if extension_values.get("Type") != "GenericPlugin":
        raise FileNotFoundError("Playnite extension.yaml type is invalid")
    if extension_values.get("Version") != payload.get("version"):
        raise FileNotFoundError(
            "Playnite bridge version does not match bundle manifest"
        )

    expected_hash = payload.get("dll_sha256")
    if not isinstance(expected_hash, str) or sha256_file(dll_path) != expected_hash:
        raise FileNotFoundError(
            "Playnite bridge DLL SHA-256 does not match bundle manifest"
        )


__all__ = [
    "PLAYNITE_EXTENSION_ID",
    "read_simple_yaml_values",
    "validate_playnite_bundle",
]
