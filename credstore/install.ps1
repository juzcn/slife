<#
.SYNOPSIS
    credstore one-click installer for Windows PowerShell.

.DESCRIPTION
    No prerequisites — the script installs uv if needed, then uses
    ``uv tool install`` to install credstore in an isolated environment.
    Python is managed automatically by uv.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/credstore/install.ps1 | iex"

    Or download first:
    irm https://raw.githubusercontent.com/juzcn/slife/main/credstore/install.ps1 -OutFile install.ps1
    .\install.ps1
#>

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Ok($msg)   { Write-Host "  $([char]0x2713) $msg" -ForegroundColor Green }
function Write-Dim($msg)  { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Err($msg)  { Write-Host $msg -ForegroundColor Red }
function Write-Warn($msg) { Write-Host $msg -ForegroundColor Yellow }

$repo = "https://github.com/juzcn/slife"

Write-Host "credstore Installer" -ForegroundColor Cyan
Write-Host ""
Write-Host "Install method : uv tool install (isolated environment)"
Write-Host "User data      : ~\.credstore\ (encrypted credential backup)"
Write-Host "Python         : managed by uv"
Write-Host ""

# [1/2] Ensure uv is available
Write-Step "[1/2] Ensuring uv is available..."
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Dim "Installing uv..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
}
Write-Ok "uv $(uv --version 2>&1)"

# [2/2] Install credstore from PyPI
Write-Step "[2/2] Installing credstore..."
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& uv tool install credstore 2>&1 | Out-Null
$ok = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEAP
if (-not $ok) {
    Write-Err "Error: credstore installation failed."
    Write-Warn "Help: $repo"
    exit 1
}
Write-Ok "credstore ready"

$localBin = "$env:USERPROFILE\.local\bin"
$env:PATH = "$localBin;$env:PATH"

# Ensure ~/.local/bin is on the persistent User PATH
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$localBin*") {
    [Environment]::SetEnvironmentVariable("Path", "$localBin;$userPath", "User")
}

Write-Host ""
Write-Host "credstore installed successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "Get started:" -ForegroundColor Cyan
Write-Host "  credstore set-password    # set up encrypted backup (first time)"
Write-Host "  credstore set API_KEY     # store a secret (masked input)"
Write-Host ""
Write-Host "More info: $repo" -ForegroundColor Cyan
