<#
.SYNOPSIS
    Slife one-click installer for Windows PowerShell.

.DESCRIPTION
    No prerequisites — the script installs Python 3.13 and uv if needed,
    then installs slife in an isolated environment.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/install.ps1 | iex"

    Or download first:
    irm https://raw.githubusercontent.com/juzcn/slife/main/install.ps1 -OutFile install.ps1
    .\install.ps1
#>

$ErrorActionPreference = "Stop"

$slifeTarball = "https://github.com/juzcn/slife/archive/refs/heads/main.zip"
$tmpDir = Join-Path $env:TEMP "slife-install-$([Guid]::NewGuid().ToString('N').Substring(0,8))"
New-Item -ItemType Directory -Force $tmpDir | Out-Null

try {
    Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "║        Slife Installer              ║" -ForegroundColor Cyan
    Write-Host "║  Terminal-based AI agent            ║" -ForegroundColor Cyan
    Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""

    # ── 1. Ensure uv is available ───────────────────────────────────────
    # uv's installer is standalone — no Python required.
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "Installing uv (package manager)…" -ForegroundColor Yellow
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
    }
    $uvVer = uv --version 2>&1
    Write-Host "√ uv $uvVer" -ForegroundColor Green

    # ── 2. Ensure Python >= 3.13 is available ───────────────────────────
    Write-Host -NoNewline "Checking for Python >= 3.13… "
    $python = $null
    foreach ($candidate in @("python3.13", "python3", "python")) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) {
            try {
                $ver = & $candidate -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
                $parts = $ver -split '\.'
                $major = [int]$parts[0]
                $minor = [int]$parts[1]
                if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 13)) {
                    $python = $candidate
                    break
                }
            } catch { }
        }
    }

    if (-not $python) {
        # Not on PATH — check if uv already manages a Python 3.13
        $uvPython = uv python find 3.13 2>$null
        if ($uvPython) {
            Write-Host "found (uv-managed)" -ForegroundColor Green
            $pythonPath = $uvPython
        } else {
            Write-Host "not found" -ForegroundColor Yellow
            Write-Host "Installing Python 3.13 via uv…" -ForegroundColor Yellow
            uv python install 3.13
            $pythonPath = uv python find 3.13 2>$null
            if (-not $pythonPath) {
                Write-Host "Error: could not install Python 3.13." -ForegroundColor Red
                Write-Host "Install manually from https://python.org/downloads/"
                exit 1
            }
            Write-Host "√ Installed at: $pythonPath" -ForegroundColor Green
        }
    } else {
        Write-Host "found" -ForegroundColor Green
        $pythonPath = (Get-Command $python).Source
    }
    $pyVer = uv run --python 3.13 python --version 2>&1
    Write-Host "  Selected: $pythonPath ($pyVer)" -ForegroundColor Cyan

    # ── 3. Download and install slife ────────────────────────────────────
    Write-Host ""
    Write-Host "Downloading slife…"

    $zipFile = Join-Path $tmpDir "slife.zip"
    Invoke-WebRequest -Uri $slifeTarball -OutFile $zipFile
    Expand-Archive -Path $zipFile -DestinationPath $tmpDir -Force

    $extractedDir = Get-ChildItem -Path $tmpDir -Directory | Select-Object -First 1

    Write-Host "Installing slife…"
    uv tool install --python 3.13 $extractedDir.FullName

    Write-Host ""
    Write-Host "══════════════════════════════════════════════" -ForegroundColor Green
    Write-Host "  Slife installed successfully! 🎉           " -ForegroundColor Green
    Write-Host "══════════════════════════════════════════════" -ForegroundColor Green
    Write-Host ""
    Write-Host "Quick start:" -ForegroundColor Cyan
    Write-Host "  credstore set-password              # set up encrypted backup (first time)"
    Write-Host "  credstore set DEEPSEEK_API_KEY       # store your API key"
    Write-Host "  slife                                # launch the TUI"
    Write-Host ""
    Write-Host "Optional extras:" -ForegroundColor Cyan
    Write-Host "  uv tool install --python 3.13 'slife[embeddings]' --reinstall"
    Write-Host "  uv tool install --python 3.13 'slife[mqtt]' --reinstall"
    Write-Host ""
    Write-Host "More info: https://github.com/juzcn/slife" -ForegroundColor Cyan

} finally {
    if (Test-Path $tmpDir) {
        Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
    }
}
