"""Construct host-validated Epic Games Launcher protocol targets."""

from __future__ import annotations

from urllib.parse import quote

from vigil_overlay.contracts.games import GameLaunchTarget, GameLaunchTargetKind


class EpicLaunchTargetFactory:
    """Build the documented launcher protocol activation target for one installed artifact."""

    @staticmethod
    def create(sandbox_id: str, catalog_id: str, artifact_id: str) -> GameLaunchTarget:
        values = (sandbox_id, catalog_id, artifact_id)
        if any(not value or value != value.strip() for value in values):
            raise ValueError(
                "Epic launch identity fields must be non-empty trimmed text"
            )
        activation_id = quote(":".join(values), safe="")
        return GameLaunchTarget(
            GameLaunchTargetKind.URI,
            f"com.epicgames.launcher://apps/{activation_id}?action=launch&silent=true",
        )


__all__ = ["EpicLaunchTargetFactory"]
