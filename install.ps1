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

    # Disable Windows Store app execution aliases that shadow real Python.
    # The "python" and "python3" commands in PowerShell often redirect to the
    # Microsoft Store instead of running an actual interpreter — even after a
    # real Python is installed.  Removing these aliases lets the real Python
    # on PATH take precedence.
    Write-Host "Removing Windows Store Python aliases…" -ForegroundColor Yellow
    foreach ($alias in @("python", "python3")) {
        $aliasPath = "$env:LOCALAPPDATA\Microsoft\WindowsApps\$alias.exe"
        if (Test-Path $aliasPath) {
            try {
                Remove-Item $aliasPath -Force -ErrorAction Stop
                Write-Host "  Removed: $aliasPath" -ForegroundColor Green
            } catch {
                Write-Host "  Could not remove $aliasPath (admin rights needed, skipped)" -ForegroundColor Yellow
            }
        }
    }
    # Refresh PATH so the freshly-installed Python takes effect
    $env:PATH = "$(Split-Path $pythonPath -Parent);$env:PATH"

    # ── 2.5 Ensure Node.js / npm is available ───────────────────────────
    Write-Host -NoNewline "Checking for Node.js / npm… "
    $haveNode = $false
    try {
        $nodeVer = node --version 2>&1
        $npmVer = npm --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "found" -ForegroundColor Green
            Write-Host "  node $nodeVer, npm $npmVer" -ForegroundColor Cyan
            $haveNode = $true
        } else {
            throw "not working"
        }
    } catch {
        Write-Host "not found" -ForegroundColor Yellow
        # Try winget first (Windows 10+/11)
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget) {
            Write-Host "Installing Node.js LTS via winget…" -ForegroundColor Yellow
            winget install OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) {
                $env:PATH = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
                try {
                    $nv = node --version 2>&1
                    Write-Host "√ node $nv installed" -ForegroundColor Green
                    $haveNode = $true
                } catch { }
            }
        }
        if (-not $haveNode) {
            Write-Host "  Skipped: fetch MCP will use Python-based article extraction." -ForegroundColor Yellow
            Write-Host "  Install manually: https://nodejs.org (LTS recommended)" -ForegroundColor Yellow
        }
    }

    # ── 3. Download and install slife ────────────────────────────────────
    Write-Host ""
    Write-Host "Downloading slife…"

    # PowerShell 5.1's Invoke-WebRequest can throw IndexOutOfRangeException
    # on GitHub's HTTP response headers.  Set TLS 1.2 and use curl.exe as
    # a fallback (curl is bundled with Windows 10 build 17063+).
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $zipFile = Join-Path $tmpDir "slife.zip"
    try {
        Invoke-WebRequest -Uri $slifeTarball -OutFile $zipFile -ErrorAction Stop
    } catch [System.IndexOutOfRangeException] {
        Write-Host "  Invoke-WebRequest failed (PowerShell 5.1 bug), trying curl.exe…" -ForegroundColor Yellow
        $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
        if ($curl) {
            curl.exe -fsSL -o $zipFile $slifeTarball
            if ($LASTEXITCODE -ne 0) {
                Write-Host "Error: curl.exe failed to download slife." -ForegroundColor Red
                exit 1
            }
        } else {
            Write-Host "Error: download failed and curl.exe not found." -ForegroundColor Red
            exit 1
        }
    }
    Expand-Archive -Path $zipFile -DestinationPath $tmpDir -Force

    $extractedDir = Get-ChildItem -Path $tmpDir -Directory | Select-Object -First 1

    # Read version from pyproject.toml
    $version = "unknown"
    $pyprojectPath = Join-Path $extractedDir.FullName "pyproject.toml"
    if (Test-Path $pyprojectPath) {
        $content = Get-Content $pyprojectPath -Raw
        if ($content -match 'version\s*=\s*"([^"]+)"') {
            $version = $matches[1]
        }
    }

    Write-Host "Building slife v$version…"
    $wheelDir = Join-Path $tmpDir "dist"
    uv build --out-dir $wheelDir $extractedDir.FullName

    $slifeWheel = Get-ChildItem -Path $wheelDir -Filter "slife-*.whl" | Select-Object -First 1
    if (-not $slifeWheel) {
        Write-Host "Error: slife wheel not found after build." -ForegroundColor Red
        exit 1
    }

    Write-Host "Installing from $($slifeWheel.Name)…"
    uv tool install --python 3.13 $slifeWheel.FullName

    Write-Host ""
    Write-Host "══════════════════════════════════════════════" -ForegroundColor Green
    Write-Host "  Slife v$version installed successfully! 🎉  " -ForegroundColor Green
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
