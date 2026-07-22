"""Provider-named alias for the shared Windows installed-app filesystem boundary."""

from vigil_overlay.providers.windows_inventory import (
    LocalWindowsInstalledAppFileSystem as LocalUbisoftFileSystem,
)
from vigil_overlay.providers.windows_inventory import (
    WindowsInstalledAppFileSystem as UbisoftFileSystem,
)

__all__ = ["LocalUbisoftFileSystem", "UbisoftFileSystem"]
