Release build note:
Run `python tools/stage_playnite_bridge.py` after compiling the native Playnite bridge and before the Nuitka build.
The staging tool places VigilOverlayBridge.dll, extension.yaml, and bridge_manifest.json here.
Vigil verifies the staged DLL SHA-256 before installing or repairing the user's Playnite extension.
