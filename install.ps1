<#
.SYNOPSIS
    Slife one-click installer for Windows PowerShell.

.DESCRIPTION
    No prerequisites — the script installs uv if needed, then uses
    ``uv tool install`` to install slife in an isolated environment.
    Python 3.13 is managed automatically by uv.

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
    Write-Host "Python            : managed by uv (3.13)"
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

    # ── 1. Ensure uv is available ──────────────────────────────────
    Write-Host "[1/5] Ensuring uv is available..." -ForegroundColor Yellow
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "  Installing uv..." -ForegroundColor Yellow
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    }
    $uvVer = uv --version 2>&1
    Write-Host "  [OK] uv $uvVer" -ForegroundColor Green

    # ── 2. Ensure npx (Node.js) is available ────────────────────────
    Write-Host "[2/5] Ensuring npx (Node.js) is available..." -ForegroundColor Yellow
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

    # ── 3. Download and verify slife ────────────────────────────────
    Write-Host "[3/5] Downloading slife..." -ForegroundColor Yellow

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

    # Read version from pyproject.toml.
    $version = "unknown"
    $pyprojectPath = Join-Path $extractedDir.FullName "pyproject.toml"
    if (Test-Path $pyprojectPath) {
        $content = Get-Content $pyprojectPath -Raw
        if ($content -match '(?m)^version\s*=\s*"([^"]+)"') {
            $version = $matches[1]
        }
    }

    # ── 4. Install slife with uv tool install ────────────────────────
    # uv tool install creates an isolated venv, installs slife + credstore
    # (workspace member), and places the executables in ~/.local/bin.
    # Python 3.13 is managed automatically by uv.
    # User data (~/.slife/) is never touched.
    Write-Host "[4/5] Installing slife v$version..." -ForegroundColor Yellow

    # Detect previously installed optional packages so we can preserve
    # them across reinstall.  Without this, "uv tool uninstall" + "uv tool
    # install" silently drops llama-cpp-python / sentence-transformers,
    # which live in optional-dependencies and are not installed by default.
    #
    # We check how each package was installed:
    #   - direct_url.json present → installed from a specific URL → replay URL
    #   - no direct_url.json      → installed from an index    → uv pip install <name>
    $preservedPackages = @()   # @{name="..."; url="..."}  or  @{name="..."}
    $slifeLine = uv tool list --show-paths 2>&1 | Select-String "slife v"
    if ($slifeLine -and $slifeLine -match '\((.+?)\)') {
        $slifeVenv = $matches[1]
        $sitePkgs = Join-Path $slifeVenv "Lib\site-packages"
        $optionalPkgs = @(
            @{import="llama_cpp";       name="llama-cpp-python"},
            @{import="sentence_transformers"; name="sentence-transformers"}
        )
        foreach ($pkg in $optionalPkgs) {
            if (Test-Path (Join-Path $sitePkgs $pkg.import)) {
                # Check if installed from a direct URL.
                $distInfo = Get-ChildItem (Join-Path $sitePkgs "$($pkg.import)-*.dist-info") -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
                $urlJson = if ($distInfo) { Join-Path $distInfo.FullName "direct_url.json" } else { $null }
                if ($urlJson -and (Test-Path $urlJson)) {
                    $url = (Get-Content $urlJson -Raw | ConvertFrom-Json).url
                    $preservedPackages += @{name=$pkg.name; url=$url}
                    Write-Host "  Detected: $($pkg.name) (from URL, will preserve)" -ForegroundColor DarkGray
                } else {
                    $preservedPackages += @{name=$pkg.name}
                    Write-Host "  Detected: $($pkg.name) (will preserve)" -ForegroundColor DarkGray
                }
            }
        }
    }

    # Clean up any previous broken installation first.
    # uv tool install will skip/error if the tool is already installed
    # in a corrupted state (missing pyvenv.cfg, etc.).
    $prevInstall = uv tool list 2>&1 | Select-String "slife"
    if ($prevInstall) {
        Write-Host "  Removing previous slife installation..." -ForegroundColor Yellow
        try { uv tool uninstall slife *>$null } catch {}
    }

    # Clean up old venv artifacts if migrating from a previous install
    # that placed the venv inside ~/.slife/.  User data (config, logs,
    # DBs, skills) is preserved.
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
    & uv tool install --from $extractedDir.FullName --python 3.13 slife > $toolInstallLog 2>&1
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEAP
    if (-not $ok) {
        Write-Host "Error: slife installation failed." -ForegroundColor Red
        Write-Host "Last lines of install log:" -ForegroundColor Yellow
        Get-Content $toolInstallLog -Tail 20
        Write-Host "Help: $slifeRepo" -ForegroundColor Yellow
        exit 1
    }

    # Re-add preserved packages into the new tool venv.
    if ($preservedPackages.Count -gt 0) {
        $newLine = uv tool list --show-paths 2>&1 | Select-String "slife v"
        if ($newLine -and $newLine -match '\((.+?)\)') {
            $newVenv = $matches[1]
            $newPython = Join-Path $newVenv "Scripts\python.exe"
            Write-Host "  Re-adding preserved packages..." -ForegroundColor Yellow
            foreach ($pkg in $preservedPackages) {
                if ($pkg.url) {
                    & uv pip install --python $newPython $pkg.url *>> $toolInstallLog
                } else {
                    & uv pip install --python $newPython $pkg.name *>> $toolInstallLog
                }
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "  Warning: failed to re-add $($pkg.name)" -ForegroundColor Yellow
                } else {
                    Write-Host "  [OK] $($pkg.name)" -ForegroundColor Green
                }
            }
        }
    }

    # ── 5. Clean up previous installation artifacts ──────────────────
    Write-Host "[5/5] Cleaning up previous installation artifacts..." -ForegroundColor Yellow

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
    if ($preservedPackages.Count -gt 0) {
        Write-Host "Preserved packages:" -ForegroundColor Cyan
        foreach ($pkg in $preservedPackages) {
            Write-Host "  [OK] $($pkg.name) (auto-detected from previous install)" -ForegroundColor Green
        }
    }
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
