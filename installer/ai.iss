; Inno Setup script for ai.
;
; Deliberately plain. This is the stock Inno wizard that thousands of Windows
; installers use: no custom pages, no bitmaps, no animation. Users recognise it
; instantly and it is the most reliable path -- every unusual thing an installer does
; is another thing that can fail on someone else's machine.
;
; Build:  python tasks.py installer
;   or:   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\ai.iss
;
; Requires dist\ai\ to exist first (python tasks.py exe).

#define AppName        "ai"
#define AppVersion     "0.1.0"
#define AppPublisher   "localai contributors"
#define AppExeName     "ai.exe"
#define AppId          "{{B7E4A2C1-9F3D-4E85-A16B-2D7C8F0E5A93}"

[Setup]
; AppId must never change between versions -- it is how Windows recognises an
; upgrade rather than a second parallel installation.
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
LicenseFile=..\LICENSE.txt
OutputDir=..\dist\installer
OutputBaseFilename=ai-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=classic
; Per-user install by default: no UAC prompt, no admin rights needed, and the
; state directory it writes to is per-user anyway. `lowest` keeps the two consistent.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add ai to my PATH (recommended)"; \
    GroupDescription: "Command line:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; \
    GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; The whole PyInstaller output folder. `recursesubdirs` matters: the _internal
; directory holds Python itself, so omitting it produces an exe that cannot start.
Source: "..\dist\ai\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md";  DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\*";     DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs
Source: "..\examples\*"; DestDir: "{app}\examples"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{#AppName} diagnostics"; Filename: "{app}\{#AppExeName}"; \
    Parameters: "doctor"; Comment: "Check that Ollama and everything else is working"
Name: "{group}\User guide"; Filename: "{app}\docs\user-guide.md"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Offered, not forced. Someone installing from a script does not want a terminal
; window opening at the end.
Filename: "{app}\{#AppExeName}"; Parameters: "doctor"; \
    Description: "Check my setup now (recommended)"; \
    Flags: postinstall shellexec skipifsilent

[Code]
{ ---------------------------------------------------------------------------
  PATH handling.

  Modifies the *user* PATH only. The machine PATH needs administrator rights and
  affects every account on the computer, which is a far larger change than
  installing a terminal tool warrants.

  Both procedures are idempotent: adding twice leaves one entry, and removing a
  path that is not present is a no-op.
  --------------------------------------------------------------------------- }

const
  UserEnvKey = 'Environment';

function PathContains(const Haystack, Needle: string): Boolean;
begin
  { Semicolon-delimit both sides so "C:\ai" does not match "C:\ai-tools". }
  Result := Pos(';' + Lowercase(Needle) + ';', ';' + Lowercase(Haystack) + ';') > 0;
end;

procedure AddToUserPath(const Directory: string);
var
  Existing: string;
begin
  if not RegQueryStringValue(HKCU, UserEnvKey, 'Path', Existing) then
    Existing := '';
  if PathContains(Existing, Directory) then
    Exit;
  if (Existing <> '') and (Copy(Existing, Length(Existing), 1) <> ';') then
    Existing := Existing + ';';
  RegWriteExpandStringValue(HKCU, UserEnvKey, 'Path', Existing + Directory);
end;

procedure RemoveFromUserPath(const Directory: string);
var
  Existing, Rebuilt, Part: string;
  Position: Integer;
begin
  if not RegQueryStringValue(HKCU, UserEnvKey, 'Path', Existing) then
    Exit;
  Rebuilt := '';
  { Rebuild the list, dropping only exact matches. Naive string replacement would
    corrupt a longer path that happens to contain ours as a substring. }
  while Length(Existing) > 0 do
  begin
    Position := Pos(';', Existing);
    if Position = 0 then
    begin
      Part := Existing;
      Existing := '';
    end
    else
    begin
      Part := Copy(Existing, 1, Position - 1);
      Existing := Copy(Existing, Position + 1, Length(Existing));
    end;
    if (Part <> '') and (Lowercase(Part) <> Lowercase(Directory)) then
    begin
      if Rebuilt <> '' then
        Rebuilt := Rebuilt + ';';
      Rebuilt := Rebuilt + Part;
    end;
  end;
  RegWriteExpandStringValue(HKCU, UserEnvKey, 'Path', Rebuilt);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddToUserPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;

  RemoveFromUserPath(ExpandConstant('{app}'));

  { Conversations, usage history, audit logs and configuration are the user's, not
    ours. Ask rather than assume, default to keeping, and say exactly where they are
    so "No" is an informed choice rather than a shrug.

    A silent uninstall is never prompted and never deletes: there is nobody there to
    answer, and destroying someone's conversation history because a deployment script
    ran with /VERYSILENT would be indefensible. Guarding on UninstallSilent is also
    what stops the uninstaller hanging on an unanswerable dialog. }
  DataDir := ExpandConstant('{localappdata}\localai');
  if DirExists(DataDir) and not UninstallSilent() then
  begin
    if MsgBox('Also delete your ai data?' + #13#10#13#10 +
              DataDir + #13#10#13#10 +
              'This contains your saved conversations, usage history, audit log and ' +
              'settings. Your Ollama models are not affected either way.' + #13#10#13#10 +
              'Choose No to keep them.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(DataDir, True, True, True);
  end;
end;
