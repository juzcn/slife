<#
.SYNOPSIS
    local-embed one-click installer for Windows PowerShell.

.DESCRIPTION
    No prerequisites — the script installs uv if needed, then uses
    ``uv tool install --force`` in an isolated environment.  --force makes
    the script idempotent: first run installs, re-runs upgrade to the latest
    PyPI release.  Python is managed automatically by uv.

    The model backend is deliberately NOT installed here — it is platform-
    specific (gguf / transformer extras, see the README for per-OS commands).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/local-embed/install.ps1 | iex"

    Or download first:
    irm https://raw.githubusercontent.com/juzcn/slife/main/local-embed/install.ps1 -OutFile install.ps1
    .\install.ps1
#>

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Ok($msg)   { Write-Host "  $([char]0x2713) $msg" -ForegroundColor Green }
function Write-Dim($msg)  { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Err($msg)  { Write-Host $msg -ForegroundColor Red }
function Write-Warn($msg) { Write-Host $msg -ForegroundColor Yellow }

$repo = "https://github.com/juzcn/slife"

Write-Host "local-embed Installer" -ForegroundColor Cyan
Write-Host ""
Write-Host "Install method : uv tool install --force (isolated environment; re-run = update)"
Write-Host "User data      : ~\.local-embed\local_embed.json5 (config)"
Write-Host "Model cache    : ~\.cache\huggingface (pre-downloaded weights)"
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

# [2/2] Install/update local-embed from PyPI.  --force: re-running this
# script upgrades an existing install (a plain ``uv tool install`` no-ops
# with "already installed").
Write-Step "[2/2] Installing/updating local-embed..."
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& uv tool install --force local-embed 2>&1 | Out-Null
$ok = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEAP
if (-not $ok) {
    Write-Err "Error: local-embed installation failed."
    Write-Warn "Help: $repo"
    exit 1
}
Write-Ok "local-embed ready"

$localBin = "$env:USERPROFILE\.local\bin"
$env:PATH = "$localBin;$env:PATH"

# Ensure ~/.local/bin is on the persistent User PATH
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$localBin*") {
    [Environment]::SetEnvironmentVariable("Path", "$localBin;$userPath", "User")
}

Write-Host ""
Write-Warn "Backends are optional extras — install per your platform (see local-embed\README.md):"
Write-Host "  uv tool install `"local-embed[gguf]`"          # llama-cpp backend (Windows: prebuilt CPU wheel)"
Write-Host "  uv tool install `"local-embed[transformer]`"   # sentence-transformers backend"
Write-Host ""
Write-Host "local-embed installed successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "Get started:" -ForegroundColor Cyan
Write-Host "  local-embed set-gguf bge-m3 --path D:\models\bge-m3.gguf   # point at a local .gguf model"
Write-Host "  local-embed                                               # serve OpenAI-compatible /v1/embeddings"
Write-Host ""
Write-Host "More info: $repo/tree/main/local-embed" -ForegroundColor Cyan