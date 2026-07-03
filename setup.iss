[Setup]
; Fixed AppId so every future version is recognized as an UPDATE of the same
; install (not a separate copy). Do not change this once you've shipped v1.
AppId={{B3C6F2A0-4C7C-4E62-9C8B-8E4A2C6D9F10}
AppName=Accountable Printing
AppVersion=1.0.1
AppPublisher=Accountable Printing
DefaultDirName={autopf}\Accountable Printing
DefaultGroupName=Accountable Printing
UninstallDisplayIcon={app}\print_app.exe
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
OutputDir=dist
OutputBaseFilename=AccountablePrinting_Installer
PrivilegesRequired=admin
; Lets "/CLOSEAPPLICATIONS" (used by the in-app auto-updater) find and close
; the running print_app.exe before overwriting files, then relaunch it.
CloseApplications=yes
RestartApplications=yes
AppMutex=AccountablePrintingAppMutex

[Files]
; Bundle the entire PyInstaller output folder (all DLLs, templates, static, etc.)
Source: "dist\print_app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Bundle the app icon
Source: "icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Desktop shortcut with Ronald's logo
Name: "{autodesktop}\Accountable Printing"; Filename: "{app}\print_app.exe"; IconFilename: "{app}\icon.ico"
; Start Menu shortcuts with Ronald's logo
Name: "{group}\Accountable Printing"; Filename: "{app}\print_app.exe"; IconFilename: "{app}\icon.ico"
Name: "{group}\Uninstall Accountable Printing"; Filename: "{uninstallexe}"

