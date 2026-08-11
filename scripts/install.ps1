param(
    [switch]$DesktopShortcut
)

$ErrorActionPreference = "Stop"
$AppName = "Local OpenRouter Chat"
$Exe = Join-Path $PSScriptRoot "LocalOpenRouterChat.exe"

if (-not (Test-Path $Exe)) {
    throw "LocalOpenRouterChat.exe was not found next to this installer. Extract the full release first."
}

$Shell = New-Object -ComObject WScript.Shell
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "$AppName.lnk"
$Shortcut = $Shell.CreateShortcut($StartMenu)
$Shortcut.TargetPath = $Exe
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.IconLocation = "$Exe,0"
$Shortcut.Description = "Private desktop chat through OpenRouter"
$Shortcut.Save()

if ($DesktopShortcut) {
    $Desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk"
    Copy-Item $StartMenu $Desktop -Force
}

Write-Host "Installed Start-menu shortcut. Search for '$AppName', then right-click it to pin it to the taskbar."
