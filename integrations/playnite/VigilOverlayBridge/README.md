# Vigil Overlay Bridge for Playnite

This optional Playnite **C#/.NET GenericPlugin** exports a read-only game snapshot for Vigil Overlay.
It replaces the earlier PowerShell bridge candidate so Vigil does not depend on Playnite's deprecated
script-extension path.

## Requirements

- Playnite with a compatible Playnite SDK 6.x plugin API.
- .NET Framework 4.6.2 support, matching Playnite's documented plugin target.
- Visual Studio/MSBuild is recommended for building the plugin.

The project references `PlayniteSDK` 6.16.0 from NuGet as a compile-time dependency only. Playnite
provides `Playnite.SDK.dll` at runtime.

## Build

From PowerShell:

```powershell
.\build.ps1 -Configuration Release
```

The expected plugin assembly is:

`bin\Release\net462\VigilOverlayBridge.dll`

## Snapshot behavior

The plugin writes:

`%AppData%\VigilOverlay\data\games\playnite_bridge.json`

Vigil reads the snapshot during its normal one-shot game-library discovery. The bridge exports only:

- Playnite library GUID
- title
- installed state
- optional absolute install directory
- optional resolved local icon path
- Playnite-owned `LastActivity` timestamp

The plugin does not export PlayCount or Playtime and does not write to the Playnite game database.
It refreshes the snapshot on application start, library update, game stop, install, and uninstall.
Snapshot publication uses a same-directory temporary file followed by atomic replacement.

Game launch remains Vigil-owned. Vigil validates the `playnite` URI scheme and asks Playnite to start
the selected library GUID.

Removing or disabling the Playnite plugin stops future snapshot refreshes. Delete
`playnite_bridge.json` to remove the last exported snapshot immediately.

## Vigil consumer packaging

Vigil users install and manage the bridge from Vigil's **Integrations** widget.

After compiling the Release DLL on the Windows release machine, stage it into Vigil's package data:

```powershell
python tools\stage_playnite_bridge.py
```

The staging step copies the DLL and extension manifest and writes a SHA-256 bundle manifest. Production Nuitka builds refuse to proceed when those staged bridge assets are missing. The resulting Vigil distribution can then install/update/repair/remove the bridge for the user without requiring the .NET SDK or manual `%AppData%` work.
