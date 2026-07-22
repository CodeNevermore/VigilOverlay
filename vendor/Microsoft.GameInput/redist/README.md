# Microsoft GameInput Redistributable staging directory

The Vigil Overlay Windows installer requires the official `GameInputRedist.msi` from Microsoft's
`Microsoft.GameInput` NuGet package. Do not replace this with a re-authored MSI or a runtime
WinGet command.

For release builds, stage the official file here as:

`vendor/Microsoft.GameInput/redist/GameInputRedist.msi`

`tools/build_installer.py` refuses to build a production installer when the prerequisite is
missing. It also requires a valid Microsoft Authenticode signature and matching GameInput MSI
product metadata. Release pipelines can add an exact SHA-256 pin with
`--approved-gameinput-sha256`. The MSI is intentionally not synthesized by the Vigil source tree.

Microsoft's GameInput Redistributable Software License Terms are preserved at
`third_party_licenses/Microsoft.GameInput/LICENSE.txt`, packaged with the standalone
distribution, and displayed by the installer for acceptance. The MSI must remain unmodified and
must not be distributed as a stand-alone Vigil download.
