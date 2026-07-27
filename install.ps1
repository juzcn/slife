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

$slifeRepo = "https://github.com/juzcn/slife"
$slifeTarball = "$slifeRepo/archive/refs/heads/main.zip"
$tmpDir = Join-Path $env:TEMP "slife-install-$([Guid]::NewGuid().ToString('N').Substring(0,8))"
New-Item -ItemType Directory -Force $tmpDir | Out-Null

try {
    Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "║        Slife Installer              ║" -ForegroundColor Cyan
    Write-Host "║  Terminal-based AI agent            ║" -ForegroundColor Cyan
    Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""

    # ── Pre-flight summary ──────────────────────────────────────────
    $installDir = "$env:USERPROFILE\.slife"
    Write-Host "Install directory : " -NoNewline
    Write-Host $installDir -ForegroundColor Cyan
    Write-Host "Python            : auto-install 3.13 if needed"
    Write-Host "npx               : auto-install Node.js if needed (required for MCP servers)"
    Write-Host "Disk space needed : ~500 MB"
    Write-Host ""

    # ── 0. Disk space check (before any download) ────────────────────
    $driveLetter = $env:USERPROFILE.Substring(0, 1)
    $freeBytes = (Get-PSDrive -Name $driveLetter -ErrorAction SilentlyContinue).Free
    if ($freeBytes -and $freeBytes -lt 1GB) {
        $freeGB = [math]::Round($freeBytes / 1GB, 1)
        Write-Host "Error: only ~${freeGB} GB free on ${driveLetter}: drive (need >= 1 GB)." -ForegroundColor Red
        Write-Host "Free up space and try again.  Help: $slifeRepo" -ForegroundColor Yellow
        exit 1
    }

    # ── 1. Ensure uvx is available (bundled with uv) ──────────────────
    Write-Host "[1/6] Checking uvx (Python package runner)..." -ForegroundColor Yellow
    if (-not (Get-Command uvx -ErrorAction SilentlyContinue)) {
        Write-Host "  Installing uv (includes uvx)..." -ForegroundColor Yellow
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
    }
    $uvxVer = uvx --version 2>&1
    Write-Host "  [OK] uvx $uvxVer" -ForegroundColor Green

    # ── 2. Ensure Python >= 3.13 is available ───────────────────────
    Write-Host "[2/6] Checking Python >= 3.13..." -ForegroundColor Yellow
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
                Write-Host "  Found $candidate ($ver) -- too old (need >= 3.13)" -ForegroundColor Yellow
            } catch { }
        }
    }

    $weInstalledPython = $false
    if (-not $python) {
        # Not on PATH -- check if uv already manages a Python 3.13
        $uvPython = (uv python find 3.13 2>$null).Trim()
        if ($uvPython) {
            Write-Host "  found (uv-managed)" -ForegroundColor Green
            $pythonPath = [System.IO.Path]::GetFullPath($uvPython)
        } else {
            Write-Host "  Python >= 3.13 not found, installing via uv..." -ForegroundColor Yellow
            uv python install 3.13
            $weInstalledPython = $true
            $uvPython = (uv python find 3.13 2>$null).Trim()
            if (-not $uvPython) {
                Write-Host "Error: could not install Python 3.13." -ForegroundColor Red
                Write-Host "Install manually from https://python.org/downloads/" -ForegroundColor Yellow
                Write-Host "Help: $slifeRepo" -ForegroundColor Yellow
                exit 1
            }
            $pythonPath = [System.IO.Path]::GetFullPath($uvPython)
            Write-Host "  [OK] Installed at: $pythonPath" -ForegroundColor Green
        }
    } else {
        Write-Host "  found" -ForegroundColor Green
        $pythonPath = [System.IO.Path]::GetFullPath((Get-Command $python).Source)
    }
    $pyVer = uv run --python "$pythonPath" python --version 2>&1
    Write-Host "  Selected: $pythonPath ($pyVer)" -ForegroundColor Cyan

    # Disable Windows Store app execution aliases that shadow real Python.
    Write-Host "  Removing Windows Store Python aliases..." -ForegroundColor Yellow
    foreach ($alias in @("python", "python3")) {
        $aliasPath = "$env:LOCALAPPDATA\Microsoft\WindowsApps\$alias.exe"
        if (Test-Path $aliasPath) {
            try {
                Remove-Item $aliasPath -Force -ErrorAction Stop
                Write-Host "    Removed: $aliasPath" -ForegroundColor Green
            } catch {
                Write-Host "    Could not remove $aliasPath (admin rights needed, skipped)" -ForegroundColor Yellow
            }
        }
    }

    # ── System-level setup — only when WE installed Python ──────────
    # If Python already existed on the system, we don't touch it.
    # The user's PATH, python, and pip are their own business.
    if ($weInstalledPython) {
        $pythonDir = Split-Path $pythonPath -Parent
        # Persist in user PATH
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if ($userPath -notlike "*$pythonDir*") {
            [Environment]::SetEnvironmentVariable("Path", "$pythonDir;$userPath", "User")
        }
        $env:PATH = "$pythonDir;$env:PATH"

        # Versioned name (python3.13) → plain "python.cmd" shim
        $pythonName = Split-Path $pythonPath -Leaf
        if ($pythonName -ne "python.exe") {
            @"
@""$pythonPath"" %*
"@ | Out-File -FilePath "$pythonDir\python.cmd" -Encoding ASCII
            Write-Host "  [OK] python ready" -ForegroundColor Green
        }

        # pip.cmd shim
        @"
@python -m pip %*
"@ | Out-File -FilePath "$pythonDir\pip.cmd" -Encoding ASCII
        Write-Host "  [OK] pip ready" -ForegroundColor Green
    }

    # ── 3. Ensure npx (Node.js) is available ────────────────────────
    Write-Host "[3/6] Checking npx (Node.js package runner)..." -ForegroundColor Yellow
    $haveNpx = $false
    if (Get-Command npx -ErrorAction SilentlyContinue) {
        try {
            $npxVer = npx --version 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  [OK] npx v$npxVer" -ForegroundColor Green
                $haveNpx = $true
            }
        } catch { }
    }

    if (-not $haveNpx) {
        Write-Host "  npx not found, installing Node.js..." -ForegroundColor Yellow
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget) {
            Write-Host "  Installing Node.js LTS via winget..." -ForegroundColor Yellow
            winget install OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) {
                $env:PATH = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
                try {
                    $nv = npx --version 2>&1
                    if ($LASTEXITCODE -eq 0) {
                        Write-Host "  [OK] npx v$nv installed" -ForegroundColor Green
                        $haveNpx = $true
                    }
                } catch { }
            }
        }

        if (-not $haveNpx) {
            Write-Host "  ┌─────────────────────────────────────────────────────┐" -ForegroundColor Red
            Write-Host "  │  WARNING: npx not available.                       │" -ForegroundColor Red
            Write-Host "  │                                                     │" -ForegroundColor Red
            Write-Host "  │  These MCP servers require npx and will NOT work:    │" -ForegroundColor Red
            Write-Host "  │    file-search, serper, tavily-mcp, github,          │" -ForegroundColor Red
            Write-Host "  │    amap-maps, filesystem                             │" -ForegroundColor Red
            Write-Host "  │                                                     │" -ForegroundColor Red
            Write-Host "  │  Install Node.js LTS from https://nodejs.org         │" -ForegroundColor Red
            Write-Host "  │  then re-run this installer.                         │" -ForegroundColor Red
            Write-Host "  └─────────────────────────────────────────────────────┘" -ForegroundColor Red
            Write-Host "Help: $slifeRepo" -ForegroundColor Yellow
            exit 1
        }
    }

    # ── Optional: Mosquitto MQTT broker (for A2A multi-agent mesh) ──
    Write-Host "[optional] Checking Mosquitto (MQTT broker for multi-agent mesh)..." -ForegroundColor Yellow
    if (Get-Command mosquitto -ErrorAction SilentlyContinue) {
        Write-Host "  [OK] mosquitto found" -ForegroundColor Green
    } else {
        Write-Host "  Mosquitto not found." -ForegroundColor Yellow
        Write-Host "  Required for: A2A multi-agent mesh communication" -ForegroundColor DarkGray
        Write-Host "  Without it:  slife works normally, just without P2P agent features" -ForegroundColor DarkGray
        try {
            $choice = Read-Host "  Install Mosquitto? (y/n, default: n)"
        } catch {
            $choice = "n"
        }
        if ($choice -eq 'y' -or $choice -eq 'Y') {
            $winget = Get-Command winget -ErrorAction SilentlyContinue
            if ($winget) {
                Write-Host "  Installing Mosquitto via winget..." -ForegroundColor Yellow
                winget install EclipseFoundation.Mosquitto --accept-package-agreements --accept-source-agreements
                if ($LASTEXITCODE -eq 0) {
                    Write-Host "  [OK] Mosquitto installed" -ForegroundColor Green
                    Write-Host "  To start Mosquitto:" -ForegroundColor Cyan
                    Write-Host "    net start mosquitto" -ForegroundColor Cyan
                    Write-Host "  Or run manually:" -ForegroundColor Cyan
                    Write-Host "    mosquitto -d -p 1883" -ForegroundColor Cyan
                } else {
                    Write-Host "  winget install failed. Install manually:" -ForegroundColor Yellow
                    Write-Host "    https://mosquitto.org/download/" -ForegroundColor Yellow
                }
            } else {
                Write-Host "  No supported package manager found (winget not available)." -ForegroundColor Yellow
                Write-Host "  Install manually: https://mosquitto.org/download/" -ForegroundColor Yellow
            }
        } else {
            Write-Host "  Skipped. Install later with: winget install EclipseFoundation.Mosquitto" -ForegroundColor DarkGray
        }
    }
    Write-Host ""

    # ── 4. Download and verify slife ────────────────────────────────
    Write-Host "[4/6] Downloading slife..." -ForegroundColor Yellow

    # PowerShell 5.1's Invoke-WebRequest can throw IndexOutOfRangeException
    # on GitHub's HTTP response headers.  Set TLS 1.2 and use curl.exe as
    # a fallback (curl is bundled with Windows 10 build 17063+).
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $zipFile = Join-Path $tmpDir "slife.zip"
    try {
        Invoke-WebRequest -Uri $slifeTarball -OutFile $zipFile -ErrorAction Stop
    } catch [System.IndexOutOfRangeException] {
        Write-Host "  Invoke-WebRequest failed (PowerShell 5.1 bug), trying curl.exe..." -ForegroundColor Yellow
        $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
        if ($curl) {
            curl.exe -fsSL -o $zipFile $slifeTarball
            if ($LASTEXITCODE -ne 0) {
                Write-Host "Error: download failed. Check your network and try again." -ForegroundColor Red
                Write-Host "Help: $slifeRepo" -ForegroundColor Yellow
                exit 1
            }
        } else {
            Write-Host "Error: download failed and curl.exe not found." -ForegroundColor Red
            Write-Host "Help: $slifeRepo" -ForegroundColor Yellow
            exit 1
        }
    }

    # Print SHA256 so users can verify integrity if desired.
    $zipHash = (Get-FileHash -Algorithm SHA256 $zipFile).Hash
    Write-Host "  SHA256: $zipHash" -ForegroundColor DarkGray

    Expand-Archive -Path $zipFile -DestinationPath $tmpDir -Force
    $extractedDir = Get-ChildItem -Path $tmpDir -Directory | Select-Object -First 1

    # Read version from pyproject.toml (anchor at line start so we only
    # match the project version, not a dependency version specifier).
    $version = "unknown"
    $pyprojectPath = Join-Path $extractedDir.FullName "pyproject.toml"
    if (Test-Path $pyprojectPath) {
        $content = Get-Content $pyprojectPath -Raw
        if ($content -match '(?m)^version\s*=\s*"([^"]+)"') {
            $version = $matches[1]
        }
    }

    # ── 5. Create venv + install slife ──────────────────────────────
    # On upgrade: user data files in ~\.slife stay put -- we only move them
    # aside while recreating the venv, then move back.
    # .credstore data is never touched by this script.
    Write-Host "[5/6] Installing slife v$version to $installDir..." -ForegroundColor Yellow
    if (Test-Path $installDir) {
        Write-Host "  Upgrading existing installation..." -ForegroundColor Yellow
        $stashDir = Join-Path $tmpDir "slife-user-stash"
        New-Item -ItemType Directory -Force $stashDir | Out-Null
        # Move user data aside; venv artifacts stay behind for deletion.
        foreach ($item in Get-ChildItem $installDir) {
            $name = $item.Name
            if ($name -ne "Scripts" -and $name -ne "Lib" -and $name -ne "Include" -and $name -ne "pyvenv.cfg") {
                Move-Item -Force $item.FullName "$stashDir\" -ErrorAction SilentlyContinue
            }
        }
        Remove-Item -Recurse -Force $installDir -ErrorAction SilentlyContinue
        uv venv --python "$pythonPath" --seed "$installDir"
        # Move user data back.
        Get-ChildItem $stashDir -ErrorAction SilentlyContinue | ForEach-Object {
            Move-Item -Force $_.FullName "$installDir\" -ErrorAction SilentlyContinue
        }
        Remove-Item -Recurse -Force $stashDir -ErrorAction SilentlyContinue
    } else {
        uv venv --python "$pythonPath" --seed "$installDir"
    }

    # Install slife from source into the venv (pip already seeded).
    Write-Host "  Installing slife and dependencies..." -ForegroundColor Yellow
    $pipLog = Join-Path $tmpDir "pip-install.log"
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & uv pip install --python "$installDir\Scripts\python.exe" $extractedDir.FullName > $pipLog 2>&1
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEAP
    if (-not $ok) {
        Write-Host "Error: slife installation failed." -ForegroundColor Red
        Write-Host "Last lines of install log:" -ForegroundColor Yellow
        Get-Content $pipLog -Tail 20
        Write-Host "Help: $slifeRepo" -ForegroundColor Yellow
        exit 1
    }

    # Verify slife and credstore are installed correctly.
    Write-Host "  Verifying installation..." -ForegroundColor Yellow
    $allOk = $true

    # Check slife package is importable.
    try {
        & "$installDir\Scripts\python.exe" -c "import slife; import credstore"
        if ($LASTEXITCODE -eq 0) {
            Write-Host "    [OK] slife + credstore packages" -ForegroundColor Green
        } else {
            Write-Host "    warning: import check failed" -ForegroundColor Yellow
            $allOk = $false
        }
    } catch {
        Write-Host "    warning: import check failed" -ForegroundColor Yellow
        $allOk = $false
    }

    # Check CLI entry points exist (don't run them — --help triggers
    # full startup which hangs if config loading blocks).
    if (Test-Path "$installDir\Scripts\credstore.exe") {
        Write-Host "    [OK] credstore CLI" -ForegroundColor Green
    } else {
        Write-Host "    warning: credstore CLI not found" -ForegroundColor Yellow
    }

    if (Test-Path "$installDir\Scripts\slife.exe") {
        Write-Host "    [OK] slife CLI" -ForegroundColor Green
    } else {
        Write-Host "    warning: slife CLI not found" -ForegroundColor Yellow
    }

    # ── 6. Create entry-point scripts (venv stays private) ──────────
    Write-Host "[6/6] Configuring entry points..." -ForegroundColor Yellow

    # Create wrapper .cmd files in ~/.local/bin (already on PATH from Step 1).
    # This exposes ONLY slife + credstore — not python, pip, or any venv internals.
    $localBin = "$env:USERPROFILE\.local\bin"
    New-Item -ItemType Directory -Force $localBin | Out-Null

    # Clean up stale .exe shims from older installs (uv tool install or
    # pre-Step-6-rework).  PATHEXT has .EXE before .CMD — an old .exe would
    # shadow our .cmd wrapper.
    foreach ($stale in @("$localBin\slife.exe", "$localBin\credstore.exe")) {
        if (Test-Path $stale) {
            Remove-Item $stale -Force
            Write-Host "  Removed stale shim: $stale" -ForegroundColor DarkGray
        }
    }

    $slifeExe   = "$installDir\Scripts\slife.exe"
    $credstoreExe = "$installDir\Scripts\credstore.exe"

    @"
@""$slifeExe"" %*
"@ | Out-File -FilePath "$localBin\slife.cmd" -Encoding ASCII

    @"
@""$credstoreExe"" %*
"@ | Out-File -FilePath "$localBin\credstore.cmd" -Encoding ASCII

    # Ensure ~/.local/bin is persisted in user PATH (Step 1 added it to
    # the current session, but not necessarily to the registry).
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$localBin*") {
        [Environment]::SetEnvironmentVariable("Path", "$localBin;$userPath", "User")
    }
    $env:PATH = "$localBin;$env:PATH"

    # Clean up stale entry from older installs that put the venv itself on PATH.
    $scriptsDir = "$installDir\Scripts"
    $cleanedPath = ($userPath -split ';' | Where-Object { $_ -ne $scriptsDir }) -join ';'
    if ($cleanedPath -ne $userPath) {
        [Environment]::SetEnvironmentVariable("Path", $cleanedPath, "User")
        Write-Host "  Cleaned up old PATH entry." -ForegroundColor DarkGray
    }

    Write-Host "  [OK] slife + credstore commands ready" -ForegroundColor Green

    Write-Host ""
    Write-Host "══════════════════════════════════════════════" -ForegroundColor Green
    Write-Host "  Slife v$version installed successfully!     " -ForegroundColor Green
    Write-Host "══════════════════════════════════════════════" -ForegroundColor Green
    Write-Host ""
    Write-Host "Get started:" -ForegroundColor Cyan
    Write-Host "  credstore set-password              # set up encrypted backup (first time)"
    Write-Host "  credstore set DEEPSEEK_API_KEY       # store your API key"
    Write-Host "  slife                                # launch the TUI"
    Write-Host ""
    Write-Host "Optional extras:" -ForegroundColor Cyan
    Write-Host "  $installDir\Scripts\pip install 'slife[gguf]' --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu"
    Write-Host "  $installDir\Scripts\pip install 'slife[transformer]'       # HuggingFace embeddings (~2 GB)"
    Write-Host ""
    Write-Host "More info: $slifeRepo" -ForegroundColor Cyan

} finally {
    if (Test-Path $tmpDir) {
        Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
    }
}
