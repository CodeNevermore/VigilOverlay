"""Provider-named aliases for the shared read-only Windows installed-app registry adapter."""

from vigil_overlay.providers.windows_inventory import (
    LocalWindowsInstalledAppRegistry as LocalUbisoftRegistry,
)
from vigil_overlay.providers.windows_inventory import WindowsInstalledAppRegistry as UbisoftRegistry
from vigil_overlay.providers.windows_inventory import (
    WindowsInstalledAppRegistryEntry as UbisoftRegistryEntry,
)

__all__ = ["LocalUbisoftRegistry", "UbisoftRegistry", "UbisoftRegistryEntry"]
