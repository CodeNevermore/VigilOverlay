# Vigil Overlay

Vigil Overlay is a controller-first Windows gaming overlay inspired by Xbox Game Bar
Compact Mode. It runs as a standalone desktop application and keeps its game-library,
telemetry, audio, display, Wi-Fi, hotkey, and input-control responsibilities behind
separate service boundaries.

## Features

- Compact controller-navigable Home, Performance, Audio, Wi-Fi, Display,
  Integrations, Settings, and Widgets surfaces.
- Native Windows CPU, GPU, VRAM, RAM, and cross-vendor FPS telemetry, with
  provider-aware game matching, sustained-GPU fallback selection, PID-scoped
  PresentMon capture, and bounded collector recovery.
- Volume, microphone, default-device, and per-application audio controls.
- Saved-profile Wi-Fi controls that do not require Windows Location access.
- Display projection, resolution, and refresh-rate controls with Keep/Revert safety.
- Recent-game discovery for Steam, Xbox / Microsoft Store, Epic Games, Battle.net,
  EA app, Ubisoft Connect, GOG, Manual Games, and the optional Playnite bridge.
- Editable global hotkey, Guide-button support, Start with Windows, background mode,
  tray controls, Safe Mode, and automatic invalid-settings recovery.
- Consistent dark/light Vigil dialogs for power, updates, shortcut editing, FPS
  errors, and confirmations, with controller-safe focus and activation handling.
- Verified foreground ownership and fail-open mouse/keyboard containment while the
  overlay is visible.
- Optional focus-preserving controller isolation through the official HidHide package
  offered by Vigil Setup or an existing installation.

Vigil Setup selects a pinned, verified official HidHide package for installation by
default when HidHide is absent; the user may clear that optional component. Setup does
not enable hiding, replace an existing HidHide version, or remove the driver when Vigil
is uninstalled. Setup accepts the prerequisite only after HidHide's official version
and path registrations and its command-line client are present. Before launching that
package, Setup writes an administrator-protected `installing` receipt and promotes it
to `pending` only after those postconditions pass. This lets a later Vigil installation
resume the one-time safe handoff if Setup is interrupted after installing HidHide.
Setup never creates the receipt merely because it finds an existing installation.
When Vigil consumes a valid pending receipt, it may add its installed executable and
only connected inputs that HidHide itself identifies as gaming devices. Vigil records
the exact configuration it created; its watchdog then adds newly connected
HidHide-verified controllers automatically, so a replacement or additional controller
does not require another trip through the HidHide client. Existing HidHide
installations are never automatically configured. Without the optional HidHide
integration, applications that independently poll XInput or subscribe to Raw Input can
still observe the physical controller while the overlay is open. Input containment is
released before Vigil hides or exits.

The **Keep the game focused** setting defaults on once an installed Windows build
detects HidHide. An explicit user choice is preserved. On a fresh HidHide installation,
Vigil completes the initial configuration automatically when at least one controller is
connected, then verifies that device hiding remains off. Previously approved controller
IDs remain managed across disconnects, and new connected controllers are added only
after HidHide identifies them as gaming devices. If no controller is connected,
automatic setup waits for a later Vigil launch. If HidHide was already installed or its
configuration is no longer clean, configure it manually: add the installed
`VigilOverlay.exe` to Applications, select only the connected gaming controller to
hide, leave inverse mode off, and turn **Enable device hiding** off.
For a composite device, expand it and select only children identified as a gamepad or
joystick. Do not select a keyboard, mouse, keypad, pointer, or a broad composite row
that contains any non-gaming child; Vigil rejects that configuration.
Vigil verifies that setup before each lease. It may add controller IDs only while the
shared HidHide configuration still exactly matches the fresh configuration Vigil
recorded; it never removes device IDs. If the user or another controller utility changes
the shared lists or mode, Vigil permanently stops automatic management and leaves those
settings untouched. A separate watchdog restores pass-through if Vigil exits
unexpectedly. From preparation through release, that watchdog is the only Vigil
process that opens HidHide's single-client control interface; the overlay process
reads its atomic verified status and heartbeat instead. Vigil remains hidden while
preparation runs. The watchdog checks for newly connected controllers every five
seconds, rechecks both Vigil-managed and manually configured leases, and restores
pass-through if hiding or the shared configuration changes. HidHide's own client tools
may maintain their own allowlist entries. Vigil uses its existing foreground mode only
after pass-through is verified; if the shared state cannot be verified, Vigil stays
hidden and directs the user to turn off device hiding.

Focus-preserving mode is intended for borderless or windowed games. A separate desktop
window cannot be guaranteed to render above every true exclusive-fullscreen game.

## Requirements

- 64-bit Windows 10 or Windows 11
- Python 3.11 or newer when running from source
- PySide6 6.7 or newer
- Optional: the official HidHide package for **Keep the game focused** controller
  isolation

End users should install Vigil with the packaged Windows installer. The installer
includes the required GameInput runtime, selects HidHide by default when absent unless
the user clears that optional component, and does not require a separate Python
installation.

Production Windows builds request administrator approval when Vigil starts so
PresentMon can capture FPS telemetry reliably. Start with Windows uses an elevated
per-user scheduled task. Release artifacts are intentionally unsigned, so Windows
may display an Unknown Publisher or SmartScreen warning.

## Running from source

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m vigil_overlay
```

Useful recovery commands:

```powershell
python -m vigil_overlay --safe-mode
python -m vigil_overlay --reset-window-position
python -m vigil_overlay --diagnose
```

Invalid settings do not block startup. Vigil preserves the unreadable or invalid file
as `settings.invalid-<UTC timestamp>.json`, restores defaults when possible, and logs
the recovery.

## Building the Windows application

Install the build dependencies and create a standalone Nuitka distribution:

```powershell
python -m pip install -e ".[build]"
python tools/build_nuitka.py --profile production
```

The build validates all required WinRT projections, the pinned PresentMon executable,
and the Playnite bridge before producing a distribution. Missing native assets are
rejected rather than silently omitted.

After staging the official Microsoft GameInput MSI and the pinned official HidHide
installer, create the installer with:

```powershell
python tools/build_installer.py `
  --gameinput-msi C:\path\to\GameInputRedist.msi `
  --hidhide-installer C:\path\to\HidHide_1.5.230_x64.exe
```

The installer builder verifies both publishers, product metadata, versions, and
approved hashes before invoking Inno Setup. HidHide is selected by default as an
optional component when no existing HidHide installation is detected, and the user
may clear that selection. Setup reads HidHide's 64-bit machine registration and
verifies its version, path, and command-line client after installation; a success exit
code without those postconditions is rejected with restart and rerun guidance. Setup
never enables device hiding. Only a fresh HidHide installed by that Setup receives the
protected pending receipt that permits Vigil's one-time, gaming-device-only initial
configuration; existing configurations remain untouched. The receipt survives an
interrupted install, is consumed after a terminal configuration decision, and is
removed with Vigil without removing HidHide itself.

## Data and privacy

Vigil reads local launcher metadata to discover installed games. It does not write its
own game-launch history. Hardware and FPS history are bounded in memory and are not
persisted. PresentMon, GameInput, and the optional HidHide installer are validated
during packaging; Vigil does not download or replace any of them while the
application is running.

Shortly after startup, Vigil makes one HTTPS request to GitHub's public latest-release
endpoint. This check only displays an update notice; Vigil does not download or install
updates automatically. Selecting Update opens the GitHub releases page and then closes
Vigil after the browser handoff succeeds. Choosing Later, or a failed browser launch,
keeps Vigil running.

Configuration, logs, integration data, and caches are stored under the current user's
Vigil Overlay application-data directories. Run `VigilOverlay --diagnose` to print the
resolved non-sensitive paths.

## Playnite integration

The optional Playnite bridge is a read-only C# GenericPlugin that publishes a bounded
snapshot for Vigil. Release builds compile and validate the bridge automatically.
Installing or removing the bridge does not modify Playnite's game database.

## Widgets and extensions

The current Widgets surface manages Vigil's built-in widgets. Third-party
`.widgetpack` installation and execution are not available yet. They remain blocked
until Vigil proves that widget processes cannot inherit its administrator token.

The planned extension model keeps popup ownership in Vigil. A widget may request a
bounded declarative dialog, but Vigil will choose its geometry and own its theme,
focus, accessibility, controller handling, actions, dismissal, and lifecycle. Widget
code will not create native dialogs or supply executable modal behavior.

## License

Vigil Overlay is open-source software distributed under the [MIT License](LICENSE).

You may use, copy, modify, merge, publish, distribute, sublicense, and sell copies of
Vigil Overlay, including as part of commercial software, provided that copies or
substantial portions include the copyright notice and MIT permission notice contained
in the `LICENSE` file.

The Vigil Overlay name, logo, and original branding are not granted for use as the
identity of modified distributions or unofficial products. Modified distributions
should use a clearly different name and visual identity. See [TRADEMARKS.md](TRADEMARKS.md).

Third-party components remain governed by their respective licenses and terms. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

This summary is provided for convenience. If it conflicts with the license text, the
license text controls.
