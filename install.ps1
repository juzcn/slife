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
    Write-Host "Install method    : uv tool install (isolated environment)"
    Write-Host "User data         : $env:USERPROFILE\.slife\"
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

    if (-not $python) {
        # Check known install locations before reaching for winget.
        # Python 3.13 may already be installed but not on PATH (e.g.
        # fresh winget install whose registry PATH hasn't propagated,
        # or Python.org installer with "Add to PATH" unchecked).
        $pyExe = $null
        $pyDir = $null
        $knownDirs = @(
            "$env:LOCALAPPDATA\Programs\Python\Python313",        # winget / python.org per-user
            "$env:ProgramFiles\Python313",                         # python.org all-users
            "$env:SystemDrive\Python313"                           # python.org root
        )
        foreach ($dir in $knownDirs) {
            $candidate = "$dir\python.exe"
            if (Test-Path $candidate) {
                $pyDir = $dir
                $pyExe = $candidate
                Write-Host "  Found Python at $pyDir (not on PATH)" -ForegroundColor Yellow
                break
            }
        }

        if (-not $pyExe) {
            Write-Host "  Python >= 3.13 not found, installing via winget..." -ForegroundColor Yellow
            $winget = Get-Command winget -ErrorAction SilentlyContinue
            if ($winget) {
                winget install Python.Python.3.13 --accept-package-agreements --accept-source-agreements
                # winget exits non-zero when the package is already installed
                # and no upgrade is available — that's fine, check the disk.
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "  winget exited with code $LASTEXITCODE (may already be installed)" -ForegroundColor Yellow
                }
                # winget installs to %LOCALAPPDATA%\Programs\Python\Python313\
                $pyDir = "$env:LOCALAPPDATA\Programs\Python\Python313"
                $pyExe = "$pyDir\python.exe"
                if (-not (Test-Path $pyExe)) {
                    # Last resort: refresh PATH and search
                    $env:PATH = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
                    $cmd = Get-Command python -ErrorAction SilentlyContinue
                    if ($cmd) {
                        $pyExe = $cmd.Source
                        $pyDir = [System.IO.Path]::GetDirectoryName($pyExe)
                    }
                }
                if (-not (Test-Path $pyExe)) {
                    Write-Host "Error: Python 3.13 not found after installation." -ForegroundColor Red
                    Write-Host "Install manually from https://python.org/downloads/" -ForegroundColor Yellow
                    exit 1
                }
            } else {
                Write-Host "Error: winget not available." -ForegroundColor Red
                Write-Host "Install Python 3.13 manually from https://python.org/downloads/" -ForegroundColor Yellow
                exit 1
            }
        }
        $pythonPath = [System.IO.Path]::GetFullPath($pyExe)
        # Ensure the Python directory is on the current session PATH
        $env:PATH = "$pyDir;$pyDir\Scripts;$env:PATH"
        Write-Host "  [OK] Python 3.13 ready" -ForegroundColor Green
    } else {
        Write-Host "  found" -ForegroundColor Green
        $pythonPath = [System.IO.Path]::GetFullPath((Get-Command $python).Source)
    }
    $pyVer = & "$pythonPath" --version 2>&1
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

    # ── 5. Install slife with uv tool install ────────────────────────
    # uv tool install creates an isolated venv, installs slife + credstore
    # (workspace member), and places the executables in ~/.local/bin.
    # User data (~/.slife/) is never touched by the installer.
    Write-Host "[5/6] Installing slife v$version..." -ForegroundColor Yellow

    # Clean up old venv artifacts if migrating from a previous install
    # that placed the venv inside ~/.slife/.  User data (config, logs,
    # DBs, skills) is preserved — we only remove venv internals.
    if (Test-Path "$env:USERPROFILE\.slife\pyvenv.cfg") {
        Write-Host "  Cleaning up old venv-based installation..." -ForegroundColor Yellow
        foreach ($venvArtifact in @("Scripts", "Lib", "Include", "pyvenv.cfg")) {
            $artifactPath = "$env:USERPROFILE\.slife\$venvArtifact"
            if (Test-Path $artifactPath) {
                Remove-Item -Recurse -Force $artifactPath -ErrorAction SilentlyContinue
            }
        }
    }

    $toolInstallLog = Join-Path $tmpDir "tool-install.log"
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & uv tool install --from $extractedDir.FullName --python "$pythonPath" slife > $toolInstallLog 2>&1
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEAP
    if (-not $ok) {
        Write-Host "Error: slife installation failed." -ForegroundColor Red
        Write-Host "Last lines of install log:" -ForegroundColor Yellow
        Get-Content $toolInstallLog -Tail 20
        Write-Host "Help: $slifeRepo" -ForegroundColor Yellow
        exit 1
    }

    # ── 6. Clean up previous installation artifacts ──────────────────
    Write-Host "[6/6] Cleaning up previous installation artifacts..." -ForegroundColor Yellow

    $localBin = "$env:USERPROFILE\.local\bin"

    # Remove stale .cmd wrappers from the old install approach.
    foreach ($stale in @("$localBin\slife.cmd", "$localBin\credstore.cmd")) {
        if (Test-Path $stale) {
            Remove-Item $stale -Force
            Write-Host "  Removed stale wrapper: $stale" -ForegroundColor DarkGray
        }
    }

    # Ensure ~/.local/bin is on PATH (uv puts tool executables here,
    # and Step 1 only added it to the current session).
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$localBin*") {
        [Environment]::SetEnvironmentVariable("Path", "$localBin;$userPath", "User")
    }
    $env:PATH = "$localBin;$env:PATH"

    # Clean up old PATH entries that pointed to the venv Scripts directory.
    $scriptsDir = "$env:USERPROFILE\.slife\Scripts"
    $cleanedPath = ($userPath -split ';' | Where-Object { $_ -ne $scriptsDir }) -join ';'
    if ($cleanedPath -ne $userPath) {
        [Environment]::SetEnvironmentVariable("Path", $cleanedPath, "User")
        Write-Host "  Removed stale PATH entry: $scriptsDir" -ForegroundColor DarkGray
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
    Write-Host "  uv tool install --with `"slife[gguf]`" slife    # local GGUF models"
    Write-Host "  uv tool install --with `"slife[transformer]`" slife  # HuggingFace embeddings (~2 GB)"
    Write-Host ""
    Write-Host "More info: $slifeRepo" -ForegroundColor Cyan

} finally {
    if (Test-Path $tmpDir) {
        Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
    }
}
