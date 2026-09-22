$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SecretsDir = Join-Path $env:LOCALAPPDATA "LocalOpenRouterChat\gateway-session"
$env:LOCAL_CHAT_SECRETS_DIR = $SecretsDir
# Include the opt-in egress overlay so its proxy container and networks are removed too.
docker compose --file (Join-Path $ProjectRoot "docker\compose.yaml") --file (Join-Path $ProjectRoot "docker\compose.egress.yaml") --profile agent down --remove-orphans

$TokenPath = Join-Path $SecretsDir "gateway_token.txt"
if (Test-Path $TokenPath) {
    Remove-Item -Force $TokenPath
}
$ExecutorTokenPath = Join-Path $SecretsDir "executor_token.txt"
if (Test-Path $ExecutorTokenPath) {
    Remove-Item -Force $ExecutorTokenPath
}
Write-Host "Containers stopped and session token files were removed."
