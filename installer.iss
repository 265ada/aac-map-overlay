; Inno Setup script for AAC Map Overlay. Built by build.ps1 / the release workflow.
; Installs per user (no admin prompt) to %LOCALAPPDATA%\Programs\AAC Map Overlay.
; Updates run this same installer with /VERYSILENT: it closes the running overlay,
; replaces the files and starts the overlay again. Settings in %APPDATA% are kept.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E7C2B1A-4F0D-4C55-9C6B-AAC0A0E1A001}
AppName=AAC Map Overlay
AppVersion={#AppVersion}
AppVerName=AAC Map Overlay {#AppVersion}
AppPublisher=265ada
AppPublisherURL=https://github.com/265ada/aac-map-overlay
AppSupportURL=https://github.com/265ada/aac-map-overlay/issues
AppUpdatesURL=https://github.com/265ada/aac-map-overlay/releases
DefaultDirName={localappdata}\Programs\AAC Map Overlay
DefaultGroupName=AAC Map Overlay
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=AACMapOverlay-Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\AACMapOverlay.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\AACMapOverlay\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; clear the old bundle so files removed in a new version don't linger
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\AAC Map Overlay"; Filename: "{app}\AACMapOverlay.exe"
Name: "{group}\Uninstall AAC Map Overlay"; Filename: "{uninstallexe}"
Name: "{userdesktop}\AAC Map Overlay"; Filename: "{app}\AACMapOverlay.exe"; Tasks: desktopicon

[Run]
; normal install: offer to start it
Filename: "{app}\AACMapOverlay.exe"; Description: "Start AAC Map Overlay"; Flags: nowait postinstall skipifsilent
; silent update: start the new version straight away
Filename: "{app}\AACMapOverlay.exe"; Flags: nowait skipifnotsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM AACMapOverlay.exe /F"; Flags: runhidden; RunOnceId: "StopOverlay"
