# Vigil Overlay

Vigil Overlay is a controller-first Windows gaming overlay inspired by Xbox Game Bar
Compact Mode. It runs as a standalone desktop application and keeps its game-library,
telemetry, audio, display, Wi-Fi, hotkey, and input-control responsibilities behind
separate service boundaries.

## Features

- Compact controller-navigable Home, Performance, Audio, Wi-Fi, Display,
  Integrations, Settings, and Widgets surfaces.
- Native Windows CPU, GPU, VRAM, RAM, and cross-vendor FPS telemetry, with
  foreground-assisted provider matching, learned verified game executables,
  PID-scoped PresentMon capture, and bounded collector recovery.
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
- Ordinary foreground-owned controller routing while Vigil is visible. Programs that
  independently poll XInput or Raw Input may still see the physical controller; Vigil
  does not claim universal controller isolation.

If Windows temporarily refuses or revokes foreground ownership, Vigil stays visible
and retries activation instead of hiding itself. Vigil remains controller-navigable
through its shared compatibility route, so the game may receive the same controller
presses until foreground ownership is verified and GameInput ownership activates.

FPS discovery captures the foreground game before Vigil opens. Exact executables
learned from earlier frame-verified sessions rank first, followed by the foreground
installed-provider match and other visible provider matches. If none match, only the
eligible foreground executable can enter a provisional probe: two sustained,
low-frequency GPU-activity samples wake it, with a 2-minute safety probe for CPU-bound
games or unavailable GPU counters. Vigil never searches for the globally busiest GPU
process.

Every new candidate remains provisional until three usable PresentMon frames verify
its current process. Vigil then learns that executable and keeps one collector through
later absent or stale frames until the process exits. A provisional candidate that
produces no usable frames is stopped and parked instead of leaving PresentMon running;
retries back off from 5 seconds to 30 seconds and then 2 minutes, while renewed
foreground activity, sustained GPU activity, provider changes, or process relaunch can
wake it sooner. Once a target verifies, candidate scanning and GPU wake checks pause.
Accepted frames update current FPS and the session average. If opening Vigil causes a
verified game to pause, Performance keeps the last current value, labels it **LAST
FPS**, and freezes the existing average. A game that keeps presenting continues to
show live values. The FPS session resets when the verified target exits.

Vigil no longer installs or uses HidHide for active controller handling. An
upgrade never uninstalls a user-owned package or rewrites its application and device lists.
If an older Vigil-authored recovery journal proves that hiding may still be active,
Vigil performs a one-time pass-through check before removing only its own legacy state.

## Requirements

- 64-bit Windows 10 or Windows 11
- Python 3.11 or newer when running from source
- PySide6 6.7 or newer

End users should install Vigil with the packaged Windows installer. The installer
includes the required GameInput runtime and does not require a separate Python
installation. It does not install or modify HidHide.

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

After staging the official Microsoft GameInput MSI, create the installer with:

```powershell
python tools/build_installer.py `
  --gameinput-msi C:\path\to\GameInputRedist.msi
```

The installer builder verifies Microsoft's signature, MSI identity, and optional
release hash pin before invoking Inno Setup.

## Data and privacy

Vigil reads local launcher metadata to discover installed games. It does not write its
own game-launch history. Hardware measurements and FPS samples are bounded in memory
and are not persisted. After a process produces valid frames, Vigil stores its local
executable path, including provider identity when available, to improve future FPS
matching. Valid unique mappings do not expire or get count-evicted during normal use;
the file has only a defensive size guard against unreasonable local data. Process IDs,
verification times or counts, and performance samples are not stored. PresentMon and
GameInput are validated during packaging; Vigil does not download or replace either one
while the application is running.

Shortly after startup, Vigil makes one HTTPS request to GitHub's public latest-release
endpoint. This check only displays an update notice; Vigil does not download or install
updates automatically. Selecting Update opens the GitHub releases page and then closes
Vigil after the browser handoff succeeds. Choosing Later, or a failed browser launch,
keeps Vigil running.

Configuration, logs, integration data, and caches are stored under the current user's
Vigil Overlay application-data directories. Logs are limited to an 8 MiB rotating
history across the active file and three backups, and managed log files older than 14
days are removed at startup. Run `VigilOverlay --diagnose` to print the resolved
non-sensitive paths.

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
