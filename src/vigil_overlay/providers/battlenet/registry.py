"""Provider-named aliases for the shared read-only Windows installed-app registry adapter."""

from vigil_overlay.providers.windows_inventory import (
    LocalWindowsInstalledAppRegistry as LocalBattleNetRegistry,
)
from vigil_overlay.providers.windows_inventory import (
    WindowsInstalledAppRegistry as BattleNetRegistry,
)
from vigil_overlay.providers.windows_inventory import (
    WindowsInstalledAppRegistryEntry as BattleNetRegistryEntry,
)

__all__ = ["BattleNetRegistry", "BattleNetRegistryEntry", "LocalBattleNetRegistry"]
