; Vigil Overlay Windows installer.
; Build through tools/build_installer.py so AppSourceDir and prerequisite paths are always supplied.

#ifndef AppSourceDir
  #error AppSourceDir must point to the Nuitka standalone directory.
#endif
#ifndef GameInputMsi
  #error GameInputMsi must point to the official Microsoft GameInputRedist.msi.
#endif
#ifndef MyAppVersion
  #define MyAppVersion "0.1.4.2"
#endif

#define MyAppName "Vigil Overlay"
#define MyAppPublisher "Vigil Overlay"
#define MyAppExeName "VigilOverlay.exe"

[Setup]
AppId={{846E722C-9C8B-4F64-93A1-74CFE8A707A4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Vigil Overlay
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputBaseFilename=VigilOverlay-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern
LicenseFile={#AppSourceDir}\licenses\third_party\Microsoft.GameInput\LICENSE.txt
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#GameInputMsi}"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\Vigil Overlay"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Vigil Overlay"; Flags: nowait postinstall skipifsilent runascurrentuser; Check: ShouldLaunchVigil

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""\VigilOverlay"" /F"; Flags: runhidden; RunOnceId: "RemoveVigilOverlayStartupTask"

[Code]
var
  PrerequisiteRestartRequired: Boolean;

function InstallGameInput(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  MsiPath: String;
begin
  Result := '';
  ExtractTemporaryFile('GameInputRedist.msi');
  MsiPath := ExpandConstant('{tmp}\GameInputRedist.msi');

  if not Exec(
    ExpandConstant('{sys}\msiexec.exe'),
    '/i "' + MsiPath + '" /qn /norestart',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
  begin
    Result := 'Vigil Overlay could not start the Microsoft GameInput prerequisite installer.';
    exit;
  end;

  if (ResultCode = 1641) or (ResultCode = 3010) then
  begin
    NeedsRestart := True;
    PrerequisiteRestartRequired := True;
  end
  else if ResultCode <> 0 then
  begin
    Result := 'Microsoft GameInput installation failed with exit code ';
    Result := Result + IntToStr(ResultCode);
    Result := Result + '. Vigil Overlay was not installed.';
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  PrerequisiteRestartRequired := False;
  Result := InstallGameInput(NeedsRestart);
end;

function ShouldLaunchVigil: Boolean;
begin
  Result := not PrerequisiteRestartRequired;
end;

procedure RemoveOwnedStartupRegistration;
var
  CurrentCommand: String;
  InstalledCommand: String;
begin
  if not RegQueryStringValue(
    HKCU,
    'Software\Microsoft\Windows\CurrentVersion\Run',
    'VigilOverlay',
    CurrentCommand
  ) then
    exit;

  InstalledCommand := '"' + ExpandConstant('{app}\{#MyAppExeName}') + '"';
  if CompareText(CurrentCommand, InstalledCommand) = 0 then
    RegDeleteValue(
      HKCU,
      'Software\Microsoft\Windows\CurrentVersion\Run',
      'VigilOverlay'
    );
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveOwnedStartupRegistration;
end;
