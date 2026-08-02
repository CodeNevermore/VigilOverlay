# Vigil Overlay

Vigil Overlay is a controller-first Windows gaming overlay inspired by Xbox Game Bar
Compact Mode. It runs as a standalone desktop application and keeps its game-library,
telemetry, audio, display, Wi-Fi, hotkey, and input-control responsibilities behind
separate service boundaries.

## Features

- Compact controller-navigable Home, Performance, Audio, Wi-Fi, Display,
  Integrations, Settings, and Widgets surfaces.
- Native Windows CPU, GPU, VRAM, RAM, and cross-vendor FPS telemetry.
- Volume, microphone, default-device, and per-application audio controls.
- Saved-profile Wi-Fi controls that do not require Windows Location access.
- Display projection, resolution, and refresh-rate controls with Keep/Revert safety.
- Recent-game discovery for Steam, Xbox / Microsoft Store, Epic Games, Battle.net,
  EA app, Ubisoft Connect, GOG, Manual Games, and the optional Playnite bridge.
- Editable global hotkey, Guide-button support, Start with Windows, background mode,
  tray controls, Safe Mode, and automatic invalid-settings recovery.
- Verified foreground ownership and fail-open mouse/keyboard containment while the
  overlay is visible.

Vigil does not use a controller filter driver. Applications that independently poll
XInput or subscribe to Raw Input can still observe the physical controller while the
overlay is open. Input containment is released before Vigil hides or exits.

## Requirements

- 64-bit Windows 10 or Windows 11
- Python 3.11 or newer when running from source
- PySide6 6.7 or newer

End users should install Vigil with the packaged Windows installer. The installer
includes the required GameInput runtime and the compiled application does not require
a separate Python installation.

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

After staging an official Microsoft `GameInputRedist.msi`, create the installer with:

```powershell
python tools/build_installer.py --gameinput-msi C:\path\to\GameInputRedist.msi
```

The installer builder verifies the MSI publisher, product metadata, and approved hash
before invoking Inno Setup.

## Data and privacy

Vigil reads local launcher metadata to discover installed games. It does not write its
own game-launch history. Hardware and FPS history are bounded in memory and are not
persisted. PresentMon and GameInput are validated during packaging; Vigil does not
download or replace either dependency while the application is running.

Shortly after startup, Vigil makes one HTTPS request to GitHub's public latest-release
endpoint. This check only displays an update notice; Vigil does not download or install
updates automatically.

Configuration, logs, integration data, and caches are stored under the current user's
Vigil Overlay application-data directories. Run `VigilOverlay --diagnose` to print the
resolved non-sensitive paths.

## Playnite integration

The optional Playnite bridge is a read-only C# GenericPlugin that publishes a bounded
snapshot for Vigil. Release builds compile and validate the bridge automatically.
Installing or removing the bridge does not modify Playnite's game database.

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
