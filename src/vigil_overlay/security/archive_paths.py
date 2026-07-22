"""Archive member path validation for host-managed widget packages."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from vigil_overlay.core.errors import UnsafeArchivePathError

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def validate_archive_member(member_name: str) -> PurePosixPath:
    """Normalize a relative archive member and reject traversal or absolute paths."""

    if not isinstance(member_name, str) or not member_name:
        raise UnsafeArchivePathError("Archive member name must be a non-empty string")
    if "\x00" in member_name:
        raise UnsafeArchivePathError("Archive member name contains a null character")

    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/") or _WINDOWS_DRIVE.match(normalized):
        raise UnsafeArchivePathError(f"Archive member is absolute: {member_name}")

    path = PurePosixPath(normalized)
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafeArchivePathError(
            f"Archive member contains an unsafe segment: {member_name}"
        )
    return path


def resolve_archive_destination(root: Path, member_name: str) -> Path:
    """Resolve a validated member beneath an extraction root."""

    relative = validate_archive_member(member_name)
    root_resolved = root.resolve()
    destination = (root_resolved / Path(*relative.parts)).resolve()
    try:
        destination.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafeArchivePathError(
            f"Archive member escapes installation root: {member_name}"
        ) from exc
    return destination
