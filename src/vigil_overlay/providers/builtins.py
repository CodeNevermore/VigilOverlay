"""Registration of trusted built-in game providers through the generic provider boundary."""

from vigil_overlay.core.paths import ApplicationPaths
from vigil_overlay.providers.battlenet import BattleNetProvider
from vigil_overlay.providers.ea import EAAppProvider
from vigil_overlay.providers.epic import EpicGamesProvider
from vigil_overlay.providers.gog import GOGProvider
from vigil_overlay.providers.manual import ManualGameProvider
from vigil_overlay.providers.playnite import PLAYNITE_BRIDGE_FILENAME, PlayniteBridgeProvider
from vigil_overlay.providers.steam import SteamProvider
from vigil_overlay.providers.ubisoft import UbisoftConnectProvider
from vigil_overlay.providers.xbox import XboxProvider
from vigil_overlay.services.game_library import GameProviderRegistration, GameProviderRegistry


def create_builtin_game_provider_registry(
    paths: ApplicationPaths,
    *,
    safe_mode: bool = False,
) -> GameProviderRegistry:
    """Register Vigil's built-in providers in deterministic aggregation order."""

    registry = GameProviderRegistry()
    if safe_mode:
        return registry
    registry.register(
        GameProviderRegistration(
            ManualGameProvider(paths.user_data_root / "games" / "manual_games.json"),
            aggregation_priority=300,
        )
    )
    registry.register(
        GameProviderRegistration(
            SteamProvider(),
            allowed_uri_schemes=("steam",),
            aggregation_priority=100,
        )
    )
    registry.register(
        GameProviderRegistration(XboxProvider(), aggregation_priority=200)
    )
    registry.register(
        GameProviderRegistration(
            EpicGamesProvider(),
            allowed_uri_schemes=("com.epicgames.launcher",),
            aggregation_priority=400,
        )
    )
    registry.register(
        GameProviderRegistration(
            BattleNetProvider(),
            aggregation_priority=500,
        )
    )
    registry.register(
        GameProviderRegistration(
            EAAppProvider(),
            aggregation_priority=600,
        )
    )
    registry.register(
        GameProviderRegistration(
            UbisoftConnectProvider(),
            aggregation_priority=700,
        )
    )
    registry.register(
        GameProviderRegistration(
            GOGProvider(),
            aggregation_priority=800,
        )
    )
    registry.register(
        GameProviderRegistration(
            PlayniteBridgeProvider(
                paths.user_data_root / "games" / PLAYNITE_BRIDGE_FILENAME
            ),
            allowed_uri_schemes=("playnite",),
            aggregation_priority=0,
        )
    )
    return registry


__all__ = ["create_builtin_game_provider_registry"]
