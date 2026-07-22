"""Provider-named alias for the shared Windows installed-app filesystem boundary."""

from vigil_overlay.providers.windows_inventory import (
    LocalWindowsInstalledAppFileSystem as LocalEAFileSystem,
)
from vigil_overlay.providers.windows_inventory import WindowsInstalledAppFileSystem as EAFileSystem

__all__ = ["EAFileSystem", "LocalEAFileSystem"]
