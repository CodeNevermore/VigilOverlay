# HidHide prerequisite staging directory

The Vigil Overlay Windows installer embeds the official HidHide 1.5.230 release
asset unchanged:

`vendor/Nefarius.HidHide/redist/HidHide_1.5.230_x64.exe`

Official release:
https://github.com/nefarius/HidHide/releases/tag/v1.5.230.0

Approved SHA-256:
`f4bbbcb82e6258641b887c74bc81c4c5f66e4aa811808dfc304347687b7605f6`

`tools/build_installer.py` refuses to build unless the asset has that exact hash, a
valid Authenticode signature from Nefarius Software Solutions e.U., and matching
HidHide product and version metadata.

The Inno installer selects HidHide by default as an optional task only when an existing
installation is not detected; users may clear that selection. It runs the official
Advanced Installer package with
`/exenoui /qn /norestart`, records restart-required exit codes, and never enables,
upgrades, downgrades, or uninstalls this shared system dependency. A successful fresh
install creates a Vigil-owned one-time marker; on first run Vigil may extend only that
clean configuration with its executable and HidHide-verified gaming-device IDs while
keeping device hiding off. Existing HidHide configurations remain untouched.

Redistribution is permitted under the MIT license retained at
`third_party_licenses/Nefarius.HidHide/LICENSE.txt`.
