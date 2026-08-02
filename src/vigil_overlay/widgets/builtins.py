"""Built-in Compact Mode widgets available before plugin runtime support."""

from __future__ import annotations

from vigil_overlay.widgets.registry import (
    DEFAULT_COMPACT_PANEL_WIDTH,
    WidgetDefinition,
    WidgetItemDefinition,
    WidgetViewKind,
)


def built_in_widget_definitions() -> tuple[WidgetDefinition, ...]:
    """Return the stable built-in widget catalog in default strip order."""

    return (
        WidgetDefinition(
            widget_id="home",
            label="Home",
            description="Your six most recent provider-reported games.",
            icon_key="home",
            items=(
                WidgetItemDefinition(
                    "power",
                    "Power",
                    "Sleep, hibernate, restart, or shut down this PC.",
                    "overlay",
                ),
            ),
            empty_message="Recent games will appear here when a game provider is connected.",
            required=True,
            preferred_panel_width=DEFAULT_COMPACT_PANEL_WIDTH,
        ),
        WidgetDefinition(
            widget_id="performance",
            label="Performance",
            description="CPU, GPU, memory, and frame-rate telemetry.",
            icon_key="performance",
            items=(
                WidgetItemDefinition("cpu", "CPU", "CPU utilization.", "computer"),
                WidgetItemDefinition("gpu", "GPU", "GPU utilization.", "computer"),
                WidgetItemDefinition(
                    "vram", "VRAM", "Video memory utilization.", "computer"
                ),
                WidgetItemDefinition(
                    "ram", "RAM", "System memory utilization.", "computer"
                ),
                WidgetItemDefinition("fps", "FPS", "Current frame rate.", "computer"),
            ),
            view_kind=WidgetViewKind.PERFORMANCE,
            preferred_panel_width=DEFAULT_COMPACT_PANEL_WIDTH,
        ),
        WidgetDefinition(
            widget_id="audio",
            label="Audio",
            description="Master audio, microphone, device, and per-app volume controls.",
            icon_key="audio",
            items=(
                WidgetItemDefinition(
                    "output_mute", "Output", "Mute or unmute system output.", "audio"
                ),
                WidgetItemDefinition(
                    "input_mute",
                    "Microphone",
                    "Mute or unmute the default microphone.",
                    "audio",
                ),
                WidgetItemDefinition(
                    "output_volume",
                    "Output volume",
                    "Adjust master system output volume.",
                    "audio",
                ),
                WidgetItemDefinition(
                    "input_volume",
                    "Microphone volume",
                    "Adjust default microphone level.",
                    "audio",
                ),
                WidgetItemDefinition(
                    "output_device",
                    "Output device",
                    "Choose the default Windows output device.",
                    "audio",
                ),
                WidgetItemDefinition(
                    "input_device",
                    "Input device",
                    "Choose the default Windows input device.",
                    "audio",
                ),
            ),
            view_kind=WidgetViewKind.AUDIO,
            preferred_panel_width=DEFAULT_COMPACT_PANEL_WIDTH,
        ),
        WidgetDefinition(
            widget_id="wifi",
            label="Wi-Fi",
            description="Windows-managed saved Wi-Fi profiles and connection controls.",
            icon_key="wifi",
            view_kind=WidgetViewKind.WIFI,
            preferred_panel_width=DEFAULT_COMPACT_PANEL_WIDTH,
        ),
        WidgetDefinition(
            widget_id="display",
            label="Display",
            description="Projection, resolution, and refresh-rate controls for this screen.",
            icon_key="display",
            items=(
                WidgetItemDefinition(
                    "projection",
                    "Projection",
                    "Choose a Windows-reported projection mode.",
                    "display",
                ),
                WidgetItemDefinition(
                    "resolution",
                    "Resolution",
                    "Choose a Windows-reported display resolution.",
                    "resolution",
                ),
                WidgetItemDefinition(
                    "refresh_rate",
                    "Refresh Rate",
                    "Choose a supported refresh rate for the selected resolution.",
                    "refresh",
                ),
            ),
            view_kind=WidgetViewKind.DISPLAY,
            preferred_panel_width=DEFAULT_COMPACT_PANEL_WIDTH,
        ),
        WidgetDefinition(
            widget_id="integrations",
            label="Integrations",
            description="Connect and manage game-library integrations used by Vigil.",
            icon_key="widgets",
            items=(
                WidgetItemDefinition(
                    "steam",
                    "Steam",
                    "Native read-only Steam discovery and connection status.",
                    "home",
                ),
                WidgetItemDefinition(
                    "xbox",
                    "Xbox / Microsoft Store",
                    "Native read-only discovery for accessible Xbox PC game installs.",
                    "home",
                ),
                WidgetItemDefinition(
                    "epic",
                    "Epic Games",
                    "Native read-only Epic Games discovery and connection status.",
                    "home",
                ),
                WidgetItemDefinition(
                    "battlenet",
                    "Battle.net",
                    "Native read-only Battle.net discovery and connection status.",
                    "home",
                ),
                WidgetItemDefinition(
                    "ea",
                    "EA app",
                    "Native read-only EA app discovery and connection status.",
                    "home",
                ),
                WidgetItemDefinition(
                    "ubisoft",
                    "Ubisoft Connect",
                    "Native read-only Ubisoft Connect discovery and connection status.",
                    "home",
                ),
                WidgetItemDefinition(
                    "gog",
                    "GOG",
                    "Native read-only GOG discovery and connection status.",
                    "home",
                ),
                WidgetItemDefinition(
                    "playnite",
                    "Playnite",
                    "Install, update, repair, or refresh the native Vigil Playnite bridge.",
                    "home",
                ),
                WidgetItemDefinition(
                    "manual_games",
                    "Manual Games",
                    "Games added directly to Vigil without another launcher.",
                    "home",
                ),
                WidgetItemDefinition(
                    "playnite_remove",
                    "Uninstall Playnite Integration",
                    "Remove Vigil's Playnite bridge and cached Playnite snapshot.",
                    "controls",
                ),
            ),
            required=True,
            view_kind=WidgetViewKind.INTEGRATIONS,
            preferred_panel_width=DEFAULT_COMPACT_PANEL_WIDTH,
        ),
        WidgetDefinition(
            widget_id="settings",
            label="Settings",
            description="Vigil Overlay behavior, controls, widgets, and recovery.",
            icon_key="settings",
            items=(
                WidgetItemDefinition(
                    "guide_button",
                    "Use controller Home/Guide button",
                    "Allow any exposed controller Home/Guide button to open or close Vigil.",
                    "controls",
                ),
                WidgetItemDefinition(
                    "controller_shortcut",
                    "Controller shortcut",
                    "Capture any exposed button or same-controller combination.",
                    "controls",
                ),
                WidgetItemDefinition(
                    "allow_mouse_navigation_while_controller_connected",
                    "Allow mouse navigation in Vigil while a controller is connected",
                    "Use a physical or controller-mapped mouse inside Vigil. When off, "
                    "Vigil uses native controller navigation. Background Raw Input/XInput "
                    "consumers may still observe the controller.",
                    "controls",
                ),
                WidgetItemDefinition(
                    "global_hotkey",
                    "Global hotkey",
                    "Choose the keyboard shortcut for opening or closing Vigil.",
                    "controls",
                ),
                WidgetItemDefinition(
                    "start_with_windows",
                    "Start Vigil with Windows",
                    "Launch Vigil automatically after you sign in to Windows.",
                    "overlay",
                ),
                WidgetItemDefinition(
                    "run_in_background",
                    "Run in background",
                    "Keep Vigil resident after the overlay is hidden so it can be "
                    "restored quickly.",
                    "overlay",
                ),
                WidgetItemDefinition(
                    "widgets",
                    "Manage Widgets",
                    "Open the Widgets tab to show or restore installed widgets.",
                    "widgets",
                ),
                WidgetItemDefinition(
                    "safe_mode",
                    "Restart in Safe Mode",
                    "Restart Vigil with temporary default settings and integrations disabled. "
                    "Your saved settings are not changed.",
                    "controls",
                ),
                WidgetItemDefinition(
                    "reset_window_position",
                    "Reset Overlay Position",
                    "Move Vigil to the primary display to recover an off-screen overlay.",
                    "controls",
                ),
            ),
            required=True,
            view_kind=WidgetViewKind.SETTINGS,
            preferred_panel_width=DEFAULT_COMPACT_PANEL_WIDTH,
        ),
        WidgetDefinition(
            widget_id="widgets",
            label="Widgets",
            description="Open or restore installed widgets.",
            icon_key="widgets",
            empty_message="No additional widgets are installed.",
            required=True,
            preferred_panel_width=DEFAULT_COMPACT_PANEL_WIDTH,
        ),
    )
