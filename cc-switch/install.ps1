<#
.SYNOPSIS
    cc-switch one-click installer for Windows PowerShell.

.DESCRIPTION
    No prerequisites — the script installs uv if needed, then uses
    ``uv tool install`` to install cc-switch in an isolated environment.
    credstore (a dependency) is pulled in automatically by uv.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/cc-switch/install.ps1 | iex"

    Or download first:
    irm https://raw.githubusercontent.com/juzcn/slife/main/cc-switch/install.ps1 -OutFile install.ps1
    .\install.ps1
#>

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Ok($msg)   { Write-Host "  $([char]0x2713) $msg" -ForegroundColor Green }
function Write-Dim($msg)  { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Err($msg)  { Write-Host $msg -ForegroundColor Red }
function Write-Warn($msg) { Write-Host $msg -ForegroundColor Yellow }

$repo = "https://github.com/juzcn/slife"

Write-Host "cc-switch Installer" -ForegroundColor Cyan
Write-Host ""
Write-Host "Install method : uv tool install (isolated environment)"
Write-Host "User data      : ~\.claude\cc-switch.json (provider/model configs)"
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

# [2/2] Install cc-switch from PyPI
Write-Step "[2/2] Installing cc-switch..."
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& uv tool install cc-switch 2>&1 | Out-Null
$ok = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEAP
if (-not $ok) {
    Write-Err "Error: cc-switch installation failed."
    Write-Warn "Help: $repo"
    exit 1
}
Write-Ok "cc-switch ready"

$localBin = "$env:USERPROFILE\.local\bin"
$env:PATH = "$localBin;$env:PATH"

# Ensure ~/.local/bin is on the persistent User PATH
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$localBin*") {
    [Environment]::SetEnvironmentVariable("Path", "$localBin;$userPath", "User")
}

Write-Host ""
Write-Host "cc-switch installed successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "Get started:" -ForegroundColor Cyan
Write-Host "  cc-switch set deepseek        # save a provider (base URL, API key name, models)"
Write-Host "  cc-switch activate deepseek   # generate ~\.claude\settings.json"
Write-Host ""
Write-Host "More info: $repo" -ForegroundColor Cyan
