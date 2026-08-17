# PeakExit deploy to mini PC (.git excluded, tar+scp)
# Example:
#   .\scripts\deploy-to-minipc.ps1 -User myuser -HostName 192.168.0.137 -RemoteDir ~/peakexit -Rebuild

param(
    [Parameter(Mandatory = $true)]
    [string] $User,
    [Parameter(Mandatory = $true)]
    [string] $HostName,
    [string] $RemoteDir = "~/peakexit",
    [switch] $Rebuild,
    [switch] $UseLegacyScp
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root "docker-compose.yml"))) {
    throw "docker-compose.yml not found: $Root"
}
Set-Location $Root

$Archive = Join-Path $env:TEMP "peakexit-deploy.tar.gz"
if (Test-Path $Archive) { Remove-Item -Force $Archive }

$exclude = @(
    ".git",
    ".env",
    ".env.*",
    "__pycache__",
    "*.pyc",
    ".venv",
    "venv",
    "env",
    "data",
    "backend/data",
    "node_modules",
    ".idea",
    ".vscode",
    "agent-tools",
    "agent-transcripts",
    "mcps",
    "terminals"
)

$tarArgs = @("-czf", $Archive)
foreach ($e in $exclude) {
    $tarArgs += "--exclude=$e"
}
$tarArgs += "."

Write-Host ">> Packaging (no .git): $Archive"
& tar @tarArgs
if ($LASTEXITCODE -ne 0) { throw "tar failed (exit $LASTEXITCODE)" }

$remote = "$User@$HostName"
$scpArgs = @()
if ($UseLegacyScp) { $scpArgs += "-O" }
$scpArgs += @($Archive, "${remote}:${RemoteDir}/peakexit-deploy.tar.gz")

Write-Host ">> scp -> ${remote}:${RemoteDir}/"
& scp @scpArgs
if ($LASTEXITCODE -ne 0) {
    throw "scp failed. Try adding -UseLegacyScp"
}

$rebuildCmd = if ($Rebuild) { "docker compose build && docker compose up -d" } else { "docker compose up -d --build" }
# Single-quoted: do not use cd '~/peakexit' — tilde does not expand in quotes (scp path != cd path).
$rdSafe = $RemoteDir.Replace("'", "'\''")
$remoteScript = @'
set -e
DEST='__REMOTE_DIR__'
DEST="${DEST/#\~/$HOME}"
mkdir -p "$DEST"
cd "$DEST"
tar -xzf peakexit-deploy.tar.gz
rm -f peakexit-deploy.tar.gz
__REBUILD_CMD__
'@ -replace '__REMOTE_DIR__', $rdSafe -replace '__REBUILD_CMD__', $rebuildCmd

Write-Host ">> Remote extract and docker compose"
$escaped = $remoteScript -replace "'", "'\''"
& ssh $remote "bash -lc '$escaped'"
if ($LASTEXITCODE -ne 0) { throw "ssh remote step failed" }

Remove-Item -Force $Archive -ErrorAction SilentlyContinue
$dashboardUrl = 'http://{0}:3000' -f $HostName
Write-Host ">> Done: $dashboardUrl"
