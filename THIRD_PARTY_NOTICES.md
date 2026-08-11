# Third-Party Notices

Last updated: August 10, 2026

Vigil Overlay includes or redistributes the components listed below. Each component
remains governed by its own license or terms. The Vigil Overlay MIT License applies
only to material that CodeNevermore has the right to license.

Complete license texts for redistributed runtime components are stored in
`third_party_licenses/`. PresentMon's license and upstream third-party notices are
kept beside its staged executable under
`src/vigil_overlay/resources/third_party/presentmon/`.

## Redistributed application and runtime components

| Component | Version or release contract | License or terms | Copyright / owner | Distribution use |
| --- | --- | --- | --- | --- |
| CPython | Python 3.11.x build runtime | Python Software Foundation License 2.0 and notices in the supplied Python license | Python Software Foundation and contributors | Nuitka standalone runtime and standard-library modules |
| PySide6, Shiboken6, and Qt | Compatible Qt for Python 6.x selected by `PySide6>=6.7,<7` | LGPL-3.0-only selected for Vigil distributions | The Qt Company Ltd., the Qt Project, and contributors | UI bindings and dynamically linked Qt libraries |
| pycaw | 20251023 | MIT | Copyright (c) 2016 AndreMiras | Windows Core Audio bindings |
| comtypes | Compatible version selected by pycaw | MIT | Copyright (c) 2006-2013 Thomas Heller; copyright (c) 2014 Comtypes Developers | COM bindings used by the audio service |
| psutil | `>=5.9,<8` | BSD-3-Clause | Copyright (c) 2009 Jay Loden, Dave Daeschler, Giampaolo Rodola | Process and system telemetry |
| PyWinRT runtime and projections | 3.2.1 | MIT | Microsoft Corporation; David Lechner and contributors | Windows Runtime networking and connectivity projections |
| PresentMon | 2.5.1 | MIT, plus upstream notices | Copyright (c) 2017-2024 Intel Corporation and identified upstream contributors | Bundled, checksum-verified FPS collector |
| Microsoft GameInput Redistributable | Official signed release MSI supplied from `Microsoft.GameInput`; the release build records the exact MSI identity | Microsoft GameInput Redistributable Software License Terms | Microsoft Corporation | Installer prerequisite |
| HidHide | 1.5.230 | MIT | Copyright (c) 2020 Eric Korff de Gidts; copyright (c) 2021-2023 Benjamin Höglinger-Stelzer | Optional installer prerequisite for controller isolation |

### Qt for Python / LGPL notice

Vigil Overlay uses the Qt for Python community distribution under LGPL-3.0-only.
PySide6, Shiboken6, and Qt are not relicensed under Vigil's MIT License. The applicable
LGPL and GPL texts are included under `third_party_licenses/Qt_for_Python/`.

Vigil's Windows distribution uses Qt as separate dynamically linked DLLs. Vigil does
not cryptographically restrict or prohibit replacement of those LGPL-covered DLLs
with compatible modified builds. Recipients may reverse engineer the application to
the extent necessary to debug modifications to the LGPL-covered libraries, as
required by the LGPL.

Corresponding Qt for Python source for the exact 6.x release selected by a binary
build is available from the official Qt for Python repository and release archives:

- https://code.qt.io/cgit/pyside/pyside-setup.git/
- https://download.qt.io/official_releases/QtForPython/

A binary publisher must record the exact PySide6/Shiboken6 version used, retain these
notices, and make the corresponding LGPL-covered source available with the release or
through a compliant written offer. Do not publish a Windows binary until that
release-specific check is complete.

### PresentMon notice

Vigil bundles the unmodified official `PresentMon-2.5.1-x64.exe` release asset and
verifies SHA-256
`9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191`
before packaging and execution. PresentMon's upstream `LICENSE.txt` and
`THIRD_PARTY.txt` are preserved beside the executable. Vigil does not download or
update PresentMon while the application is running.

Upstream project: https://github.com/GameTechDev/PresentMon/tree/v2.5.1

### Microsoft GameInput notice

The Windows installer redistributes the official, Microsoft-signed
`GameInputRedist.msi` unchanged. Its separate Microsoft Software License Terms apply
to that prerequisite and are reproduced at
`third_party_licenses/Microsoft.GameInput/LICENSE.txt`. The installer displays those
terms for acceptance before installation. GameInput is not covered by Vigil's MIT
License.

Official package: https://www.nuget.org/packages/Microsoft.GameInput/

### HidHide notice

The Windows installer embeds the unmodified official, Nefarius-signed
`HidHide_1.5.230_x64.exe` release asset and verifies SHA-256
`f4bbbcb82e6258641b887c74bc81c4c5f66e4aa811808dfc304347687b7605f6`
before packaging. Its MIT license is reproduced at
`third_party_licenses/Nefarius.HidHide/LICENSE.txt`.

HidHide is offered as a selected-by-default optional component only when no existing
installation is detected; users may clear that selection. Installation does not enable
device hiding. Only when that Setup installed a fresh HidHide may Vigil perform a
one-time extension of the clean configuration with its executable and device IDs that
HidHide reports as connected gaming input. Existing configurations are not changed.
Vigil's uninstaller leaves HidHide installed so it cannot break another application
that also uses the driver.

Official release: https://github.com/nefarius/HidHide/releases/tag/v1.5.230.0

## Optional integration dependency

The Vigil Overlay Bridge is Vigil-owned MIT-licensed code. It is compiled against
`PlayniteSDK` 6.16.0, which is MIT licensed and copyright © Josef Nemec. The SDK is a
compile-time-only dependency and is not copied into the bridge bundle; Playnite
provides its SDK assembly at runtime. The PlayniteSDK license is retained under
`third_party_licenses/PlayniteSDK/`.

## Build and installer tools

The following tools are used to create releases but are not application runtime
dependencies distributed as Python packages:

| Tool | Declared version contract | License | Release note |
| --- | --- | --- | --- |
| Nuitka | `>=2.6,<3` | GNU AGPL-3.0 with an exception for created binaries | The compiler itself is not included in Vigil source or binary packages; generated binaries are covered by Nuitka's stated exception. |
| ordered-set | `>=4.1` | MIT | Build dependency used by Nuitka; not shipped as a Vigil runtime dependency. |
| zstandard | `>=0.22` | BSD-3-Clause | Build dependency used by Nuitka; not shipped as a Vigil runtime dependency. |
| Inno Setup | Release-machine installation | Inno Setup License | Used to create the Windows installer; its license is retained under `third_party_licenses/Inno_Setup/`. |

Nuitka licensing information and its created-binary exception:
https://nuitka.net/pages/overview.html

Inno Setup licensing information:
https://jrsoftware.org/isinfo.php

## Platform APIs and third-party services

Vigil calls Windows, XInput, GameInput, Core Audio, WLAN, display, and other operating
system APIs. Except for the separately redistributed GameInput and optional HidHide
installers described above, those operating-system components are not copied into
this repository.

Steam, Xbox / Microsoft Store, Epic Games, Battle.net, EA app, Ubisoft Connect, GOG,
and Playnite names and local data formats are used only for interoperability. Vigil
does not include their launcher code, SDKs, artwork, or game assets. Those names are
trademarks or service marks of their respective owners.

The original Vigil Overlay icon and other project-specific visual assets are owned by
CodeNevermore. Third-party game icons may be displayed only when read locally from a
user's installed software; Vigil does not redistribute them.

## Binary release verification

This source-level inventory covers the dependencies selected by the repository. A
binary publisher must also inspect the final Nuitka standalone directory and installer
for compiler runtimes, Qt plugins, codec or platform plugins, and any other generated
files not named above. Every additional redistributed component must have its license
and notices added before publication.

Questions about Vigil-owned material may be directed to CodeNevermore through the
official Vigil Overlay repository.
