#define MyAppName "Discord Conversation Processor"
#define MyAppVersion "0.0.0"
#define MyAppExeName "ChatForge-v" + MyAppVersion + "-win64-portable.exe"
#define MySetupBase "ChatForge-v" + MyAppVersion + "-win64-setup"
#define MyAppId "9C6E6E8A-9E2C-4A9E-8B90-76B7D7D3B7E2"
#define MyAppIdBraced "{{" + MyAppId + "}}"

[Setup]
AppId={#MyAppIdBraced}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppName}
DefaultDirName={commonpf}\Discord Conversation Processor
DefaultGroupName={#MyAppName}
OutputBaseFilename={#MySetupBase}
OutputDir=..\..\dist_installer
SetupIconFile=..\..\exe\app_logo.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\app_logo.ico

[Tasks]
Name: "startmenuicon"; Description: "Create a &Start Menu shortcut"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a &desktop icon"; Flags: unchecked
Name: "uninstallentry"; Description: "Create an &Uninstall entry"; Flags: checkedonce

[Files]
Source: "..\..\dist\{#MyAppExeName}"; DestDir: "{app}"; DestName: "ChatForge.exe"; Flags: ignoreversion
Source: "..\..\exe\app_logo.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\ChatForge.exe"; Tasks: startmenuicon; IconFilename: "{app}\app_logo.ico"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\ChatForge.exe"; Tasks: desktopicon; IconFilename: "{app}\app_logo.ico"

[Run]
Filename: "{app}\ChatForge.exe"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  UninstallKey: string;
  UninstallExe: string;
  UninstallDat: string;
begin
  if CurStep = ssPostInstall then
  begin
    if not WizardIsTaskSelected('uninstallentry') then
    begin
      UninstallKey := ExpandConstant('Software\Microsoft\Windows\CurrentVersion\Uninstall\{' + MyAppId + '}_is1');
      RegDeleteKeyIncludingSubkeys(HKLM, UninstallKey);
      RegDeleteKeyIncludingSubkeys(HKCU, UninstallKey);
      UninstallExe := ExpandConstant('{uninstallexe}');
      UninstallDat := ChangeFileExt(UninstallExe, '.dat');
      DeleteFile(UninstallExe);
      DeleteFile(UninstallDat);
    end;
  end;
end;
