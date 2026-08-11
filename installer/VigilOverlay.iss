; Vigil Overlay Windows installer.
; Build through tools/build_installer.py so AppSourceDir and prerequisite paths are always supplied.

#ifndef AppSourceDir
  #error AppSourceDir must point to the Nuitka standalone directory.
#endif
#ifndef GameInputMsi
  #error GameInputMsi must point to the official Microsoft GameInputRedist.msi.
#endif
#ifndef HidHideInstaller
  #error HidHideInstaller must point to the approved official HidHide installer.
#endif
#ifndef MyAppVersion
  #define MyAppVersion "0.1.2.3"
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
Source: "{#HidHideInstaller}"; Flags: dontcopy

[Tasks]
Name: "hidhide"; Description: "Install HidHide for Keep the game focused (recommended; may require a restart)"; GroupDescription: "Optional components:"; Check: not IsHidHideInstalled

[Icons]
Name: "{autoprograms}\Vigil Overlay"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Vigil Overlay"; Flags: nowait postinstall skipifsilent runascurrentuser; Check: ShouldLaunchVigil

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""\VigilOverlay"" /F"; Flags: runhidden; RunOnceId: "RemoveVigilOverlayStartupTask"

[UninstallDelete]
Type: files; Name: "{app}\.vigil-hidhide-fresh-install.json"

[Code]
var
  PrerequisiteRestartRequired: Boolean;
  HidHideInstalledByVigil: Boolean;

function IsHidHideInstalled: Boolean;
var
  InstalledVersion: String;
  InstallPath: String;
begin
  Result := RegQueryStringValue(
    HKCR,
    'Installer\Dependencies\NSS.Drivers.HidHide.x64',
    'Version',
    InstalledVersion
  ) and (Trim(InstalledVersion) <> '');
  if Result then
    exit;

  Result := RegQueryStringValue(
    HKCR,
    'SOFTWARE\Nefarius Software Solutions e.U.\Nefarius Software Solutions e.U. HidHide',
    'Path',
    InstallPath
  ) and (Trim(InstallPath) <> '');
end;

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

function InstallHidHide(var NeedsRestart: Boolean): String;
var
  InstallerPath: String;
  ResultCode: Integer;
begin
  Result := '';
  if not WizardIsTaskSelected('hidhide') or IsHidHideInstalled then
    exit;

  ExtractTemporaryFile('HidHide_1.5.230_x64.exe');
  InstallerPath := ExpandConstant('{tmp}\HidHide_1.5.230_x64.exe');
  if not Exec(
    InstallerPath,
    '/exenoui /qn /norestart',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
  begin
    Result := 'Vigil Overlay could not start the HidHide prerequisite installer.';
    exit;
  end;

  if (ResultCode = 0) or (ResultCode = 1641) or (ResultCode = 3010) then
    HidHideInstalledByVigil := True;

  if (ResultCode = 1641) or (ResultCode = 3010) then
  begin
    NeedsRestart := True;
    PrerequisiteRestartRequired := True;
  end
  else if ResultCode <> 0 then
  begin
    Result := 'HidHide installation failed with exit code ';
    Result := Result + IntToStr(ResultCode);
    Result := Result + '. Vigil Overlay was not installed.';
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  PrerequisiteRestartRequired := False;
  Result := InstallGameInput(NeedsRestart);
  if Result <> '' then
    exit;

  Result := InstallHidHide(NeedsRestart);
end;

function ShouldLaunchVigil: Boolean;
begin
  Result := not PrerequisiteRestartRequired;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  MarkerPath: String;
begin
  if (CurStep <> ssPostInstall) or not HidHideInstalledByVigil then
    exit;

  MarkerPath := ExpandConstant('{app}\.vigil-hidhide-fresh-install.json');
  if not SaveStringToFile(
    MarkerPath,
    '{"schema":1,"source":"VigilOverlay Setup","hidhide_version":"1.5.230"}',
    False
  ) then
    Log('Could not write the fresh HidHide configuration marker.');
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
