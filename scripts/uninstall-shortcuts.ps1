$AppName = "Local OpenRouter Chat"
$Paths = @(
    (Join-Path ([Environment]::GetFolderPath("Programs")) "$AppName.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk")
)

foreach ($Path in $Paths) {
    if (Test-Path $Path) {
        Remove-Item $Path -Force
    }
}

Write-Host "Shortcuts removed. Local chat data was left intact."
