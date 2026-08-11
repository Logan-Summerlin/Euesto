#define AppName "Local OpenRouter Chat"
#define AppVersion "1.1.0"

[Setup]
AppId={{BDFB95B6-CA86-4F23-9F17-0A6D0B6F5E11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Local OpenRouter Chat
AppPublisherURL=https://github.com/Logan-Summerlin/Local-Chatbot
DefaultDirName={localappdata}\Programs\LocalOpenRouterChat
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\dist\installer
OutputBaseFilename=LocalOpenRouterChat-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\assets\app.ico
ChangesAssociations=no

[Files]
Source: "..\dist\LocalOpenRouterChat\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\LocalOpenRouterChat.exe"; WorkingDir: "{app}"; IconFilename: "{app}\assets\app.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\LocalOpenRouterChat.exe"; WorkingDir: "{app}"; IconFilename: "{app}\assets\app.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\LocalOpenRouterChat.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
