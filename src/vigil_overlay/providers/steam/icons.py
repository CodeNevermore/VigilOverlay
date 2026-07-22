"""Resolve local Steam artwork or an unambiguous executable icon."""

from __future__ import annotations

from pathlib import PureWindowsPath

from vigil_overlay.contracts.games import GameDiscoveryContext, GameIconKind, GameIconReference
from vigil_overlay.providers.steam.filesystem import SteamFileSystem

_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".ico"})
_DIRECT_CACHE_NAMES = (
    "{app_id}_icon.jpg",
    "{app_id}_icon.png",
    "{app_id}_icon.ico",
    "{app_id}_library_600x900.jpg",
    "{app_id}_library_600x900.png",
    "{app_id}_library_header.jpg",
    "{app_id}_library_header.png",
    "{app_id}_header.jpg",
    "{app_id}_header.png",
)
_NESTED_CACHE_NAMES = (
    "icon.jpg",
    "icon.png",
    "icon.ico",
    "library_600x900.jpg",
    "library_600x900.png",
    "library_header.jpg",
    "library_header.png",
    "header.jpg",
    "header.png",
)


class SteamIconResolver:
    """Resolve a local Steam artwork file or unambiguous executable icon."""

    def __init__(self, filesystem: SteamFileSystem) -> None:
        self._filesystem = filesystem

    def resolve(
        self,
        steam_root: str,
        app_id: str,
        install_directory: str,
        context: GameDiscoveryContext,
    ) -> GameIconReference | None:
        artwork = self._resolve_cached_artwork(steam_root, app_id, context)
        if artwork is not None:
            return GameIconReference(GameIconKind.LOCAL_IMAGE, artwork)

        pattern = str(PureWindowsPath(install_directory) / "*.exe")
        try:
            executables = tuple(
                path
                for path in self._filesystem.glob(pattern, context)
                if PureWindowsPath(path).name.casefold()
                not in {"unins000.exe", "uninstall.exe"}
            )
        except (OSError, TimeoutError):
            return None
        if len(executables) == 1:
            return GameIconReference(GameIconKind.EXECUTABLE, executables[0])
        return None

    def _resolve_cached_artwork(
        self,
        steam_root: str,
        app_id: str,
        context: GameDiscoveryContext,
    ) -> str | None:
        cache_root = PureWindowsPath(steam_root) / "appcache" / "librarycache"
        for template in _DIRECT_CACHE_NAMES:
            candidate = str(cache_root / template.format(app_id=app_id))
            try:
                if self._filesystem.is_file(candidate, context):
                    return candidate
            except (OSError, TimeoutError):
                return None

        nested_root = cache_root / app_id
        for name in _NESTED_CACHE_NAMES:
            candidate = str(nested_root / name)
            try:
                if self._filesystem.is_file(candidate, context):
                    return candidate
            except (OSError, TimeoutError):
                return None

        # Recent Steam clients may place hashed asset filenames inside an app-ID
        # subdirectory. Prefer image files whose names indicate an icon before
        # falling back to another local library artwork image. Never fetch artwork
        # from the network; provider discovery remains local and read-only.
        try:
            nested_files = self._filesystem.glob(str(nested_root / "*"), context)
        except (OSError, TimeoutError):
            return None
        images = tuple(
            path
            for path in nested_files
            if PureWindowsPath(path).suffix.casefold() in _IMAGE_SUFFIXES
        )
        if not images:
            return None
        for path in images:
            if "icon" in PureWindowsPath(path).stem.casefold():
                return path
        return images[0]


__all__ = ["SteamIconResolver"]
