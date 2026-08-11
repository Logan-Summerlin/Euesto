param(
    [Parameter(Mandatory=$false)]
    [string]$Workspace
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SecretsDir = Join-Path $env:LOCALAPPDATA "LocalOpenRouterChat\gateway-session"
New-Item -ItemType Directory -Force -Path $SecretsDir | Out-Null
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function New-SessionToken {
    $Bytes = New-Object byte[] 32
    $Rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Rng.GetBytes($Bytes) } finally { $Rng.Dispose() }
    return [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

$Token = New-SessionToken
[System.IO.File]::WriteAllText((Join-Path $SecretsDir "gateway_token.txt"), $Token, $Utf8NoBom)
[System.IO.File]::WriteAllText((Join-Path $SecretsDir "executor_token.txt"), (New-SessionToken), $Utf8NoBom)
$env:LOCAL_CHAT_SECRETS_DIR = $SecretsDir
$Compose = Join-Path $ProjectRoot "docker\compose.yaml"

if ($Workspace) {
    $Resolved = (Resolve-Path -LiteralPath $Workspace -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $Resolved -PathType Container)) { throw "Workspace must be a directory." }
    if ($Resolved -eq [System.IO.Path]::GetPathRoot($Resolved) -or $Resolved -eq $env:USERPROFILE) {
        throw "Drive and profile roots are forbidden workspaces."
    }
    $env:LOCAL_CHAT_WORKSPACE = $Resolved
    $Normalized = $Resolved.ToLowerInvariant()
    $Sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $env:LOCAL_CHAT_WORKSPACE_ID = ([BitConverter]::ToString($Sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Normalized)))).Replace("-", "").ToLowerInvariant()
    } finally { $Sha.Dispose() }
    docker compose --file $Compose --profile agent up --detach --build
    Write-Host "Gateway and no-network executor started for: $Resolved"
} else {
    Remove-Item Env:LOCAL_CHAT_WORKSPACE -ErrorAction SilentlyContinue
    Remove-Item Env:LOCAL_CHAT_WORKSPACE_ID -ErrorAction SilentlyContinue
    docker compose --file $Compose up --detach --build
    Write-Host "Chat-only gateway started."
}
Set-Clipboard -Value $Token
Write-Host "Gateway: http://127.0.0.1:8765"
Write-Host "The gateway token was copied to the clipboard."
