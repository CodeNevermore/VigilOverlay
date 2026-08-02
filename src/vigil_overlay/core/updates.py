"""Release metadata parsing and version comparison for update checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

GITHUB_LATEST_RELEASE_API = (
    "https://api.github.com/repos/CodeNevermore/VigilOverlay/releases/latest"
)
GITHUB_RELEASES_URL = "https://github.com/CodeNevermore/VigilOverlay/releases"

_VERSION_PATTERN = re.compile(r"[vV]?(\d+(?:\.\d+)*)\Z")


class ReleaseMetadataError(ValueError):
    """Raised when a GitHub release response cannot be compared safely."""


@dataclass(frozen=True, slots=True)
class AvailableUpdate:
    """A stable GitHub release newer than the running application."""

    current_version: str
    latest_version: str
    release_name: str


def parse_release_version(value: str) -> tuple[int, ...]:
    """Parse the numeric release tags used by Vigil Overlay."""

    match = _VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ReleaseMetadataError(
            f"release version must contain only dot-separated numbers: {value!r}"
        )
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer_release(candidate: str, current: str) -> bool:
    """Return whether ``candidate`` is numerically newer than ``current``."""

    candidate_parts = parse_release_version(candidate)
    current_parts = parse_release_version(current)
    width = max(len(candidate_parts), len(current_parts))
    return candidate_parts + (0,) * (width - len(candidate_parts)) > (
        current_parts + (0,) * (width - len(current_parts))
    )


def available_update_from_github(
    payload: Any,
    *,
    current_version: str,
) -> AvailableUpdate | None:
    """Convert GitHub's latest-release payload into an available update."""

    if not isinstance(payload, dict):
        raise ReleaseMetadataError("GitHub release response must be an object")
    if payload.get("draft") is True or payload.get("prerelease") is True:
        return None

    tag_name = payload.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name.strip():
        raise ReleaseMetadataError("GitHub release response has no tag name")
    tag_name = tag_name.strip()
    if not is_newer_release(tag_name, current_version):
        return None

    name = payload.get("name")
    release_name = name.strip() if isinstance(name, str) and name.strip() else tag_name
    return AvailableUpdate(
        current_version=current_version,
        latest_version=tag_name,
        release_name=release_name,
    )


__all__ = [
    "GITHUB_LATEST_RELEASE_API",
    "GITHUB_RELEASES_URL",
    "AvailableUpdate",
    "ReleaseMetadataError",
    "available_update_from_github",
    "is_newer_release",
    "parse_release_version",
]
