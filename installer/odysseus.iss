; Odysseus installer — wraps the PyInstaller standalone bundle
; (dist\OdysseusStandalone) into a distributable setup.
; Build:  ISCC.exe installer\odysseus.iss   (output: installer\Output\OdysseusSetup.exe)

#define MyAppName "Odysseus"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "Z-Gamez"
#define MyAppURL "https://github.com/Z-Gamez/odysseus-roundtable"
#define MyAppExeName "Odysseus.exe"

[Setup]
AppId={{8B1F1A52-6E1C-4C79-9D5B-3A7C41E90D62}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
; Per-user install: no admin/UAC needed (like VS Code / Discord).
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=OdysseusSetup
SetupIconFile=..\static\odysseus.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; The bundle is ~320MB; give the wizard an honest estimate.
ExtraDiskSpaceRequired=350000000

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "startupicon"; Description: "Start Odysseus automatically when you log in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\OdysseusStandalone\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Program files only — user data at %USERPROFILE%\.odysseus is preserved
; deliberately (models/chats/settings survive reinstalls).
Type: filesandordirs; Name: "{app}"

[Code]
// Local AI models need Ollama. Detect a typical install; if absent, offer the
// download page at the end of setup. Odysseus itself runs fine without it
// (API-based models still work) so this is informational, not blocking.
function OllamaInstalled(): Boolean;
begin
  Result :=
    FileExists(ExpandConstant('{localappdata}\Programs\Ollama\ollama.exe')) or
    FileExists(ExpandConstant('{pf}\Ollama\ollama.exe')) or
    RegKeyExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\Ollama');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    if not OllamaInstalled() then begin
      if MsgBox('Odysseus uses Ollama to run local AI models (optional — cloud API models work without it).' + #13#10#13#10 +
                'Ollama does not appear to be installed. Open the Ollama download page now?',
                mbConfirmation, MB_YESNO) = IDYES then
        ShellExec('open', 'https://ollama.com/download', '', '', SW_SHOW, ewNoWait, ErrCode);
    end;
  end;
end;
