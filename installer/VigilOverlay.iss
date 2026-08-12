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
  #define MyAppVersion "0.1.2.4"
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
const
  HidHideRegistryKey = 'SOFTWARE\Nefarius Software Solutions e.U.\HidHide';
  HidHideReceiptKey = 'SOFTWARE\Vigil Overlay\Prerequisites\HidHide';
  HidHideReceiptSource = 'VigilOverlay Setup';
  HidHideReceiptVersion = '1.5.230';

var
  PrerequisiteRestartRequired: Boolean;
  HidHideInstalledByVigil: Boolean;

function HidHideCliExists(InstallPath: String): Boolean;
var
  Root: String;
begin
  Root := AddBackslash(InstallPath);
  Result := FileExists(Root + 'HidHideCLI.exe') or
    FileExists(Root + 'x64\HidHideCLI.exe') or
    FileExists(Root + 'bin\HidHideCLI.exe') or
    FileExists(Root + 'bin\x64\HidHideCLI.exe');
end;

function IsHidHideInstalled: Boolean;
var
  InstalledVersion: String;
  InstallPath: String;
  HasVersion: Boolean;
  HasPath: Boolean;
begin
  HasVersion := RegQueryStringValue(
    HKLM,
    HidHideRegistryKey,
    'Version',
    InstalledVersion
  ) and (Trim(InstalledVersion) <> '');
  HasPath := RegQueryStringValue(
    HKLM,
    HidHideRegistryKey,
    'Path',
    InstallPath
  ) and (Trim(InstallPath) <> '');
  Result := HasVersion and HasPath and HidHideCliExists(InstallPath);
end;

procedure RemoveHidHideInstallReceipt;
begin
  RegDeleteKeyIncludingSubkeys(HKLM, HidHideReceiptKey);
end;

function IsHidHideInstallReceiptState(ExpectedState: String): Boolean;
var
  Schema: Cardinal;
  Source: String;
  HidHideVersion: String;
  State: String;
begin
  Result := RegQueryDWordValue(HKLM, HidHideReceiptKey, 'Schema', Schema) and
    (Schema = 1) and
    RegQueryStringValue(HKLM, HidHideReceiptKey, 'Source', Source) and
    (Source = HidHideReceiptSource) and
    RegQueryStringValue(
      HKLM,
      HidHideReceiptKey,
      'HidHideVersion',
      HidHideVersion
    ) and
    (HidHideVersion = HidHideReceiptVersion) and
    RegQueryStringValue(HKLM, HidHideReceiptKey, 'State', State) and
    (State = ExpectedState);
end;

function WriteInstallingHidHideReceipt: Boolean;
begin
  RemoveHidHideInstallReceipt;
  Result := RegWriteDWordValue(HKLM, HidHideReceiptKey, 'Schema', 1) and
    RegWriteStringValue(
      HKLM,
      HidHideReceiptKey,
      'Source',
      HidHideReceiptSource
    ) and
    RegWriteStringValue(
      HKLM,
      HidHideReceiptKey,
      'HidHideVersion',
      HidHideReceiptVersion
    ) and
    RegWriteStringValue(HKLM, HidHideReceiptKey, 'State', 'installing');
end;

function PromoteHidHideReceiptToPending: Boolean;
begin
  if IsHidHideInstallReceiptState('pending') then
  begin
    Result := True;
    exit;
  end;
  Result := IsHidHideInstallReceiptState('installing') and
    RegWriteStringValue(HKLM, HidHideReceiptKey, 'State', 'pending');
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
  if IsHidHideInstalled then
  begin
    if IsHidHideInstallReceiptState('installing') then
    begin
      if not PromoteHidHideReceiptToPending then
      begin
        Result := 'Vigil Overlay could not finalize its protected HidHide receipt.';
        exit;
      end;
    end;
    HidHideInstalledByVigil := IsHidHideInstallReceiptState('pending');
    exit;
  end;
  if not WizardIsTaskSelected('hidhide') then
    exit;

  if not WriteInstallingHidHideReceipt then
  begin
    Result := 'Vigil Overlay could not create its protected HidHide install receipt.';
    exit;
  end;

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
    RemoveHidHideInstallReceipt;
    Result := 'Vigil Overlay could not start the HidHide prerequisite installer.';
    exit;
  end;

  Log('HidHide prerequisite installer exit code: ' + IntToStr(ResultCode));

  if (ResultCode = 0) or (ResultCode = 1641) or (ResultCode = 3010) then
  begin
    if not IsHidHideInstalled then
    begin
      Result := 'HidHide reported a successful installation, but its required ';
      Result := Result + 'registration and command-line client were not detected. ';
      Result := Result + 'Restart Windows, then rerun Vigil Setup.';
      exit;
    end;
    if not PromoteHidHideReceiptToPending then
    begin
      Result := 'Vigil Overlay could not finalize its protected HidHide receipt.';
      exit;
    end;
    HidHideInstalledByVigil := True;
  end;

  if (ResultCode = 1641) or (ResultCode = 3010) then
  begin
    NeedsRestart := True;
    PrerequisiteRestartRequired := True;
  end
  else if ResultCode <> 0 then
  begin
    RemoveHidHideInstallReceipt;
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
  begin
    RemoveOwnedStartupRegistration;
    RemoveHidHideInstallReceipt;
  end;
end;
