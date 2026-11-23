; Per-user installer for Etabli Captation (stage 11, ADR-005). No administrator rights needed.
; OBS Studio is NOT bundled: install it from https://obsproject.com/download
;
; Built by packaging\windows\build.ps1 -Installer (Inno Setup 6.3 or later):
;   ISCC.exe /DAppVersion=<v> /DSourceDir=<dist\captation> /DOutputDir=<dist>
;            /DOutputBaseName=<name> packaging\windows\etabli-captation.iss
; The file is ASCII on purpose (no encoding questions for ISCC).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\captation"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef OutputBaseName
  #define OutputBaseName "etabli-captation-setup"
#endif
; AppUserModelID for toasts with buttons (PF-223); also registered by the agent itself.
#define AppUserModelId "de.etabli.captation"

[Setup]
; Fixed AppId: upgrades replace the previous installation. Never change it.
AppId={{E3D27606-70C2-4B6A-8A61-3581C0DA84E8}
AppName=Etabli Captation
AppVersion={#AppVersion}
AppPublisher=Etabli
AppComments=Records calls through OBS Studio only while study mode is armed, with prior written consent.
; Per-user install without UAC: {autopf} = %LOCALAPPDATA%\Programs (PF-270)
PrivilegesRequired=lowest
DefaultDirName={autopf}\Etabli Captation
DefaultGroupName=Etabli Captation
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseName}
LicenseFile={#SourceDir}\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Etabli Captation
UninstallDisplayIcon={app}\captation.exe
; The agent holds files while it runs: Setup asks to close it (Restart Manager).
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "autostart"; Description: "Start Etabli Captation at login (it always starts disarmed)"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; The shortcut carries the AppUserModelID so toasts are attributed to the app (PF-223).
Name: "{autoprograms}\Etabli Captation"; Filename: "{app}\captationw.exe"; Parameters: "run"; WorkingDir: "{app}"; AppUserModelID: "{#AppUserModelId}"; Comment: "Records calls only while study mode is armed"

[Run]
Filename: "{app}\captation.exe"; Parameters: "install-autostart"; Tasks: autostart; Flags: runhidden waituntilterminated; StatusMsg: "Registering the logon task..."
; Disclaimer checkbox (R-59): without it the agent never arms. Shown after setup, checked by default.
Filename: "{app}\captationw.exe"; Parameters: "accept"; WorkingDir: "{app}"; Description: "Read and accept the disclaimer and conditions of use (required before first use)"; Flags: postinstall skipifsilent waituntilterminated
Filename: "{app}\captationw.exe"; Parameters: "run"; WorkingDir: "{app}"; Description: "Start Etabli Captation now (disarmed)"; Flags: nowait postinstall skipifsilent unchecked

[UninstallRun]
; Removes the Task Scheduler task and the AppUserModelID key (whatever install-autostart created).
Filename: "{app}\captation.exe"; Parameters: "uninstall-autostart"; Flags: runhidden waituntilterminated; RunOnceId: "UninstallAutostart"

[Registry]
; The agent registers the AUMID key at runtime; make sure uninstall removes it, without creating it here.
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\{#AppUserModelId}"; ValueType: none; Flags: uninsdeletekey dontcreatekey
