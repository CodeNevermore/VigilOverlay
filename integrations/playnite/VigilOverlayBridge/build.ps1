param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$project = Join-Path $PSScriptRoot "VigilOverlayBridge.csproj"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$stageScript = Join-Path $projectRoot "tools\stage_playnite_bridge.py"

if (Get-Command msbuild.exe -ErrorAction SilentlyContinue) {
    & msbuild.exe $project /restore /t:Build /p:Configuration=$Configuration
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
elif (Get-Command dotnet.exe -ErrorAction SilentlyContinue) {
    & dotnet.exe build $project --configuration $Configuration
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
else {
    throw "MSBuild or the .NET SDK is required to build the Playnite plugin. Visual Studio with .NET Framework 4.6.2 targeting support is recommended."
}

if ($Configuration -eq "Release") {
    if (-not (Test-Path $stageScript)) {
        throw "Playnite bridge built, but Vigil staging script was not found: $stageScript"
    }

    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        & py.exe -3 $stageScript
    }
    elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
        & python.exe $stageScript
    }
    else {
        throw "Playnite bridge built, but Python was not found to stage the DLL into Vigil resources."
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Playnite bridge compiled but automatic Vigil resource staging failed."
    }
}

exit 0
