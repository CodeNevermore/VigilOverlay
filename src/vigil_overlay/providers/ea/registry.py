"""Provider-named aliases for the shared read-only Windows installed-app registry adapter."""

from vigil_overlay.providers.windows_inventory import (
    LocalWindowsInstalledAppRegistry as LocalEARegistry,
)
from vigil_overlay.providers.windows_inventory import WindowsInstalledAppRegistry as EARegistry
from vigil_overlay.providers.windows_inventory import (
    WindowsInstalledAppRegistryEntry as EARegistryEntry,
)

__all__ = ["EARegistry", "EARegistryEntry", "LocalEARegistry"]
