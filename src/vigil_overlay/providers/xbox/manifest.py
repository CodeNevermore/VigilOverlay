"""Parse MicrosoftGame.config files from local XboxGames flat-file installs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath
from xml.etree import ElementTree

from vigil_overlay.contracts.games import GameDiscoveryContext
from vigil_overlay.providers.xbox.filesystem import XboxFileSystem

_IMAGE_SUFFIXES = {".bmp", ".ico", ".jpeg", ".jpg", ".png"}


@dataclass(frozen=True, slots=True)
class XboxGameManifest:
    """Normalized local metadata needed by the native Xbox provider."""

    provider_game_id: str
    title: str
    install_directory: str
    executable_path: str
    icon_path: str | None = None


class XboxGameManifestScanner:
    """Scan bounded XboxGames layouts and tolerate malformed individual titles."""

    def __init__(self, filesystem: XboxFileSystem) -> None:
        self._filesystem = filesystem

    def scan(
        self,
        roots: tuple[str, ...],
        context: GameDiscoveryContext,
    ) -> tuple[tuple[XboxGameManifest, ...], tuple[str, ...]]:
        manifests: list[XboxGameManifest] = []
        warnings: list[str] = []
        seen_paths: set[str] = set()
        seen_ids: set[str] = set()

        for root in roots:
            if context.is_cancelled():
                break
            patterns = (
                str(PureWindowsPath(root, "*", "Content", "MicrosoftGame.config")),
                str(PureWindowsPath(root, "*", "MicrosoftGame.config")),
            )
            for pattern in patterns:
                for path in self._filesystem.glob(pattern, context):
                    if context.is_cancelled():
                        break
                    path_key = path.casefold()
                    if path_key in seen_paths:
                        continue
                    seen_paths.add(path_key)
                    try:
                        manifest = self._parse(path, context)
                    except (
                        OSError,
                        TimeoutError,
                        ValueError,
                        ElementTree.ParseError,
                    ) as exc:
                        warnings.append(
                            f"Could not read Xbox game manifest {path}: {exc}"
                        )
                        continue
                    game_key = manifest.provider_game_id.casefold()
                    if game_key in seen_ids:
                        continue
                    seen_ids.add(game_key)
                    manifests.append(manifest)

        return tuple(manifests), tuple(warnings)

    def _parse(self, path: str, context: GameDiscoveryContext) -> XboxGameManifest:
        raw = self._filesystem.read_text(path, context)
        root = ElementTree.fromstring(raw)
        if _local_name(root.tag) != "Game":
            raise ValueError("MicrosoftGame.config root element must be Game")

        identity = _first_child(root, "Identity")
        identity_name = _clean_value(
            identity.get("Name") if identity is not None else None
        )
        store_id_node = _first_child(root, "StoreId")
        store_id = _clean_value(
            store_id_node.text if store_id_node is not None else None
        )
        provider_game_id = store_id or identity_name
        if provider_game_id is None:
            raise ValueError(
                "MicrosoftGame.config is missing StoreId and Identity Name"
            )
        if len(provider_game_id) > 256:
            raise ValueError("Xbox game identity exceeds 256 characters")

        executable = _select_pc_executable(root)
        if executable is None:
            raise ValueError(
                "MicrosoftGame.config has no non-development PC executable"
            )
        executable_name = _safe_relative_path(
            executable.get("Name"), required_suffix=".exe"
        )
        if executable_name is None:
            raise ValueError("MicrosoftGame.config executable path is invalid")

        manifest_path = PureWindowsPath(path)
        install_directory = manifest_path.parent
        executable_path = PureWindowsPath(install_directory, executable_name)

        shell_visuals = _first_child(root, "ShellVisuals")
        title = _first_display_value(
            executable.get("OverrideDisplayName"),
            (
                shell_visuals.get("DefaultDisplayName")
                if shell_visuals is not None
                else None
            ),
            _install_folder_name(install_directory),
            identity_name,
        )
        if title is None:
            raise ValueError(
                "MicrosoftGame.config does not provide a usable game title"
            )

        icon_path = self._resolve_icon(
            executable, shell_visuals, install_directory, context
        )
        return XboxGameManifest(
            provider_game_id=provider_game_id,
            title=title,
            install_directory=str(install_directory),
            executable_path=str(executable_path),
            icon_path=icon_path,
        )

    def _resolve_icon(
        self,
        executable: ElementTree.Element,
        shell_visuals: ElementTree.Element | None,
        install_directory: PureWindowsPath,
        context: GameDiscoveryContext,
    ) -> str | None:
        values = [
            executable.get("OverrideSquare480x480Logo"),
            executable.get("OverrideLogo"),
            executable.get("OverrideSquare44x44Logo"),
        ]
        if shell_visuals is not None:
            values.extend(
                (
                    shell_visuals.get("Square480x480Logo"),
                    shell_visuals.get("Square150x150Logo"),
                    shell_visuals.get("Square44x44Logo"),
                    shell_visuals.get("StoreLogo"),
                )
            )
        for value in values:
            relative = _safe_relative_path(value, allowed_suffixes=_IMAGE_SUFFIXES)
            if relative is None:
                continue
            candidate = str(PureWindowsPath(install_directory, relative))
            if self._filesystem.is_file(candidate, context):
                return candidate
        return None


def _select_pc_executable(root: ElementTree.Element) -> ElementTree.Element | None:
    executable_list = _first_child(root, "ExecutableList")
    if executable_list is None:
        return None
    for executable in executable_list:
        if _local_name(executable.tag) != "Executable":
            continue
        is_dev_only = (executable.get("IsDevOnly") or "false").strip().casefold()
        if is_dev_only == "true":
            continue
        target_family = (executable.get("TargetDeviceFamily") or "").strip().casefold()
        if target_family and target_family != "pc":
            continue
        if (
            _safe_relative_path(executable.get("Name"), required_suffix=".exe")
            is not None
        ):
            return executable
    return None


def _safe_relative_path(
    value: str | None,
    *,
    required_suffix: str | None = None,
    allowed_suffixes: set[str] | None = None,
) -> str | None:
    cleaned = _clean_value(value)
    if cleaned is None:
        return None
    path = PureWindowsPath(cleaned)
    if (
        path.is_absolute()
        or path.drive
        or path.root
        or any(part == ".." for part in path.parts)
    ):
        return None
    suffix = path.suffix.casefold()
    if required_suffix is not None and suffix != required_suffix.casefold():
        return None
    if allowed_suffixes is not None and suffix not in allowed_suffixes:
        return None
    return str(path)


def _first_child(root: ElementTree.Element, name: str) -> ElementTree.Element | None:
    for child in root:
        if _local_name(child.tag) == name:
            return child
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or "\x00" in cleaned or "\r" in cleaned or "\n" in cleaned:
        return None
    return cleaned


def _first_display_value(*values: str | None) -> str | None:
    for value in values:
        cleaned = _clean_value(value)
        if cleaned is None:
            continue
        lowered = cleaned.casefold()
        if lowered.startswith("ms-resource:") or cleaned.startswith("**"):
            continue
        return cleaned
    return None


def _install_folder_name(install_directory: PureWindowsPath) -> str | None:
    candidate = install_directory
    if candidate.name.casefold() == "content":
        candidate = candidate.parent
    return _clean_value(candidate.name)


__all__ = ["XboxGameManifest", "XboxGameManifestScanner"]
