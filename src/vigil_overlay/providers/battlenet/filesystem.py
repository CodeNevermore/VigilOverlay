"""Provider-named alias for the shared Windows installed-app filesystem boundary."""

from vigil_overlay.providers.windows_inventory import (
    LocalWindowsInstalledAppFileSystem as LocalBattleNetFileSystem,
)
from vigil_overlay.providers.windows_inventory import (
    WindowsInstalledAppFileSystem as BattleNetFileSystem,
)

__all__ = ["BattleNetFileSystem", "LocalBattleNetFileSystem"]
