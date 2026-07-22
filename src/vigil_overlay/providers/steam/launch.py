"""Construct host-validated Steam protocol launch targets."""

from vigil_overlay.contracts.games import GameLaunchTarget, GameLaunchTargetKind


class SteamLaunchTargetFactory:
    """Create validated Steam protocol targets from numeric application IDs."""

    @staticmethod
    def create(app_id: str) -> GameLaunchTarget:
        if not app_id.isdigit():
            raise ValueError("Steam app ID must be numeric")
        return GameLaunchTarget(GameLaunchTargetKind.URI, f"steam://rungameid/{app_id}")


__all__ = ["SteamLaunchTargetFactory"]
