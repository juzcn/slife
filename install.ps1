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

# Helpers
function Write-Step($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Ok($msg)   { Write-Host "  $([char]0x2713) $msg" -ForegroundColor Green }
function Write-Dim($msg)  { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Warn($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host $msg -ForegroundColor Red }

# Extract bare package name from a pip-freeze line.
#   name==1.0  → name
#   name @ url → name
function Get-PkgName($spec) {
    ($spec -replace '\s*@.+$', '' -replace '==.+$', '').Trim().ToLower()
}

# Parse the venv path from "uv tool list --show-paths" output.
# Returns the path inside parentheses, or $null.
function Get-SlifeVenv {
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $line = & uv tool list --show-paths 2>$null | Select-String "slife v"
    $ErrorActionPreference = $prevEAP
    if ($line -and $line -match '\((.+?)\)') { return $matches[1] }
    return $null
}

# Constants
$slifeRepo    = "https://github.com/juzcn/slife"
$slifeTarball = "$slifeRepo/archive/refs/heads/main.zip"
$tmpDir       = Join-Path $env:TEMP "slife-install-$([Guid]::NewGuid().ToString('N').Substring(0,8))"
New-Item -ItemType Directory -Force $tmpDir | Out-Null

try {
    Write-Host "Slife Installer" -ForegroundColor Cyan
    Write-Host ""

    # Pre-flight summary
    Write-Host "Install method    : uv tool install (isolated environment)"
    Write-Host "User data         : $env:USERPROFILE\.slife\"
    Write-Host "Python            : managed by uv (3.13)"
    Write-Host "npx               : auto-install Node.js if needed (required for MCP servers)"
    Write-Host "Disk space needed : ~500 MB"
    Write-Host ""

    # 0. Disk space check
    $driveLetter = $env:USERPROFILE.Substring(0, 1)
    $freeBytes   = (Get-PSDrive -Name $driveLetter -ErrorAction SilentlyContinue).Free
    if ($freeBytes -and $freeBytes -lt 1GB) {
        $freeGB = [math]::Round($freeBytes / 1GB, 1)
        Write-Err "Error: only ~${freeGB} GB free on ${driveLetter}: drive (need >= 1 GB)."
        Write-Warn "Free up space and try again.  Help: $slifeRepo"
        exit 1
    }

    # 1. Ensure uv is available
    Write-Step "[1/5] Ensuring uv is available..."
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Dim "Installing uv..."
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    }
    Write-Ok "uv $(uv --version 2>&1)"

    # 2. Ensure npx (Node.js) is available
    Write-Step "[2/5] Ensuring npx (Node.js) is available..."
    $haveNpx = $false
    if (Get-Command npx -ErrorAction SilentlyContinue) {
        try {
            $npxVer = npx --version 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "npx v$npxVer"
                $haveNpx = $true
            }
        } catch { }
    }

    if (-not $haveNpx) {
        Write-Dim "npx not found, installing Node.js..."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Dim "Installing Node.js LTS via winget..."
            winget install OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) {
                $env:PATH = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                            [System.Environment]::GetEnvironmentVariable("Path", "User")
                try {
                    $nv = npx --version 2>&1
                    if ($LASTEXITCODE -eq 0) {
                        Write-Ok "npx v$nv installed"
                        $haveNpx = $true
                    }
                } catch { }
            }
        }

        if (-not $haveNpx) {
            Write-Err "WARNING: npx not available."
            Write-Err "  These MCP servers require npx and will NOT work:"
            Write-Err "    file-search, serper, tavily-mcp, github, amap-maps, filesystem"
            Write-Err "  Install Node.js LTS from https://nodejs.org then re-run this installer."
            Write-Warn "Help: $slifeRepo"
            exit 1
        }
    }

    # Optional: Mosquitto MQTT broker
    Write-Step "[optional] Checking Mosquitto (MQTT broker for multi-agent mesh)..."
    if (Get-Command mosquitto -ErrorAction SilentlyContinue) {
        Write-Ok "mosquitto found"
    } else {
        Write-Warn "  Mosquitto not found."
        Write-Dim "  Required for: A2A multi-agent mesh communication"
        Write-Dim "  Without it:  slife works normally, just without P2P agent features"
        try { $choice = Read-Host "  Install Mosquitto? (y/n, default: n)" } catch { $choice = "n" }
        if ($choice -eq 'y' -or $choice -eq 'Y') {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                Write-Dim "Installing Mosquitto via winget..."
                winget install EclipseFoundation.Mosquitto --accept-package-agreements --accept-source-agreements
                if ($LASTEXITCODE -eq 0) {
                    Write-Ok "Mosquitto installed"
                    Write-Host "  To start Mosquitto:" -ForegroundColor Cyan
                    Write-Host "    net start mosquitto"
                    Write-Host "  Or run manually:"
                    Write-Host "    mosquitto -d -p 1883"
                } else {
                    Write-Warn "  winget install failed. Install manually:"
                    Write-Warn "    https://mosquitto.org/download/"
                }
            } else {
                Write-Warn "  No supported package manager found (winget not available)."
                Write-Warn "  Install manually: https://mosquitto.org/download/"
            }
        } else {
            Write-Dim "  Skipped. Install later with: winget install EclipseFoundation.Mosquitto"
        }
    }
    Write-Host ""

    # 3. Download and verify slife
    Write-Step "[3/5] Downloading slife..."

    # PowerShell 5.1's Invoke-WebRequest can throw IndexOutOfRangeException
    # on GitHub's HTTP response headers.  Fall back to curl.exe (bundled
    # with Windows 10 build 17063+) when that happens.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $zipFile = Join-Path $tmpDir "slife.zip"
    try {
        Invoke-WebRequest -Uri $slifeTarball -OutFile $zipFile -ErrorAction Stop
    } catch [System.IndexOutOfRangeException] {
        Write-Warn "  Invoke-WebRequest failed (PowerShell 5.1 bug), trying curl.exe..."
        if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
            curl.exe -fsSL -o $zipFile $slifeTarball
            if ($LASTEXITCODE -ne 0) {
                Write-Err "Error: download failed. Check your network and try again."
                Write-Warn "Help: $slifeRepo"
                exit 1
            }
        } else {
            Write-Err "Error: download failed and curl.exe not found."
            Write-Warn "Help: $slifeRepo"
            exit 1
        }
    }

    $zipHash = (Get-FileHash -Algorithm SHA256 $zipFile).Hash
    Write-Dim "  SHA256: $zipHash"

    Expand-Archive -Path $zipFile -DestinationPath $tmpDir -Force
    $extractedDir = Get-ChildItem -Path $tmpDir -Directory | Select-Object -First 1

    # Read pyproject.toml once (version + extra-index-url)
    $version       = "unknown"
    $extraIndexArgs = @()
    $pyprojectPath = Join-Path $extractedDir.FullName "pyproject.toml"
    if (Test-Path $pyprojectPath) {
        $pyprojectContent = Get-Content $pyprojectPath -Raw

        # Version: [project] / version = "x.y.z"
        if ($pyprojectContent -match '(?m)^version\s*=\s*"([^"]+)"') {
            $version = $matches[1]
        }

        # Extra index URL: [tool.uv] / extra-index-url = "..." or ["..."]
        $urlRe = 'extra-index-url\s*=\s*(?:\[\s*)?["'']([^"''\]]+)'
        $urlMatch = [regex]::Match($pyprojectContent, $urlRe)
        if ($urlMatch.Success) {
            $extraIndexArgs = @("--extra-index-url", $urlMatch.Groups[1].Value)
        }
    }

    # 4. Install slife with uv tool install
    Write-Step "[4/5] Installing slife v$version..."

    # Capture user-installed packages from the old venv so we can re-add
    # them after the fresh install.  "uv tool uninstall" + "uv tool install"
    # silently drops everything beyond core dependencies.
    $preservedReqs  = Join-Path $env:TEMP "slife-preserved-requirements.txt"
    $preservedFull  = Join-Path $env:TEMP "slife-preserved-full.txt"
    Set-Content -Path $preservedReqs -Value "" -Encoding utf8
    Set-Content -Path $preservedFull -Value "" -Encoding utf8

    $oldVenv = Get-SlifeVenv
    if ($oldVenv) {
        $oldPython = Join-Path $oldVenv "Scripts\python.exe"
        if (Test-Path $oldPython) {
            Write-Dim "Capturing installed packages from previous installation..."

            # Capture full freeze (with versions), filtering out:
            #   - editable installs (-e ...)
            #   - slife / credstore (reinstalled fresh)
            #   - stderr noise (no == or @ — not a valid freeze line)
            & uv pip freeze --python $oldPython 2>$null |
                Where-Object { $_ -notmatch '^-e ' -and $_ -notmatch '^(slife|credstore)\s*[@=]' } |
                Where-Object { $_ -match '==' -or $_ -match '@' } |
                Out-File -Encoding utf8 $preservedFull

            # Derive name-only list from the full freeze (no dual-write sync issues).
            Get-Content $preservedFull |
                ForEach-Object { Get-PkgName $_ } |
                Where-Object { $_ } |
                Out-File -Encoding utf8 $preservedReqs

            $pkgCount = (Get-Content $preservedReqs).Count
            if ($pkgCount -gt 0) {
                Write-Dim "Captured $pkgCount packages — will diff against fresh install to find user-added ones"
            }
        }
    }

    # Kill any running slife processes — they hold locks on the venv.
    $slifeProcs = Get-Process -Name "slife","python" -ErrorAction SilentlyContinue |
                  Where-Object { $_.Path -like "*slife*" -or $_.Path -like "*uv\tools\slife*" }
    if ($slifeProcs) {
        Write-Warn "Stopping running slife processes..."
        $slifeProcs | ForEach-Object {
            try { Stop-Process -Id $_.Id -Force -ErrorAction Stop; Write-Dim "  Stopped $($_.ProcessName) (PID $($_.Id))" }
            catch { Write-Warn "  Could not stop $($_.ProcessName) (PID $($_.Id))" }
        }
        Start-Sleep -Seconds 2  # Let file handles release
    }

    # Remove previous installation (clean slate for uv tool install).
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $installed = uv tool list 2>$null | Select-String "slife"
    $ErrorActionPreference = $prevEAP
    if ($installed) {
        Write-Dim "Removing previous slife installation..."
        $uninstallLog = Join-Path $tmpDir "uninstall.log"
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & uv tool uninstall slife 2>&1 > $uninstallLog
        $ErrorActionPreference = $prevEAP
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "  uv tool uninstall reported errors (continuing):"
            Get-Content $uninstallLog -Tail 5 | ForEach-Object { Write-Dim "    $_" }
        }
    }

    # uv tool uninstall may leave the venv directory behind on Windows
    # when a process (antivirus, leftover slife, etc.) holds a lock.
    # Clean it up explicitly — rename out of the way if removal fails
    # so the subsequent uv tool install has a clean slate.
    $oldToolDir = "$env:APPDATA\uv\tools\slife"
    if (Test-Path $oldToolDir) {
        try {
            Remove-Item -Recurse -Force $oldToolDir -ErrorAction Stop
            Write-Dim "Cleaned up old tool venv: $oldToolDir"
        } catch {
            # Directory is locked — rename it so the new install can proceed.
            $backupDir = "$oldToolDir.old.$(Get-Date -Format 'yyyyMMddHHmmss')"
            Write-Warn "Cannot remove locked directory, renaming to:"
            Write-Dim "  $backupDir"
            try {
                Rename-Item $oldToolDir $backupDir -ErrorAction Stop
            } catch {
                Write-Err "Error: could not remove or rename old slife tool directory."
                Write-Warn "Close any running slife windows and try again."
                Write-Warn "Or manually delete: $oldToolDir"
                Write-Warn "Help: $slifeRepo"
                exit 1
            }
        }
    }

    # Remove stale shims in ~/.local/bin — uv tool install refuses to
    # overwrite an existing .exe that a previous (failed) install left.
    $localBin = "$env:USERPROFILE\.local\bin"
    if (Test-Path $localBin) {
        foreach ($stale in @("$localBin\slife.exe", "$localBin\slife.cmd",
                             "$localBin\credstore.exe", "$localBin\credstore.cmd")) {
            if (Test-Path $stale) {
                Remove-Item $stale -Force -ErrorAction SilentlyContinue
                Write-Dim "Removed stale shim: $stale"
            }
        }
    }

    # Clean up old venv artifacts if migrating from a previous install
    # that placed the venv inside ~/.slife/.  User data is preserved.
    if (Test-Path "$env:USERPROFILE\.slife\pyvenv.cfg") {
        Write-Dim "Cleaning up old venv-based installation..."
        foreach ($artifact in @("Scripts", "Lib", "Include", "pyvenv.cfg")) {
            $artifactPath = "$env:USERPROFILE\.slife\$artifact"
            if (Test-Path $artifactPath) {
                Remove-Item -Recurse -Force $artifactPath -ErrorAction SilentlyContinue
            }
        }
    }

    # Install.
    $toolInstallLog = Join-Path $tmpDir "tool-install.log"
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & uv tool install --from $extractedDir.FullName --python 3.13 slife 2>&1 > $toolInstallLog
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEAP
    if (-not $ok) {
        Write-Err "Error: slife installation failed."
        Write-Warn "Last lines of install log:"
        Get-Content $toolInstallLog -Tail 20
        Write-Warn "Help: $slifeRepo"
        exit 1
    }

    # Re-add preserved packages
    $preserveOk = $false
    if ((Test-Path $preservedReqs) -and ((Get-Content $preservedReqs).Count -gt 0)) {
        $newVenv = Get-SlifeVenv
        if ($newVenv) {
            $newPython = Join-Path $newVenv "Scripts\python.exe"

            # Diff: packages in old venv but not in the new base install.
            $newFreeze = & uv pip freeze --python $newPython 2>$null |
                ForEach-Object { ($_ -split '==')[0].Trim().ToLower() } |
                Where-Object { $_ }
            $oldPkgs = Get-Content $preservedReqs |
                ForEach-Object { $_.Trim().ToLower() } |
                Where-Object { $_ -notin $newFreeze }

            $extraCount = $oldPkgs.Count
            if ($extraCount -eq 0) {
                Write-Dim "All packages already present — nothing to re-add"
                $preserveOk = $true
                # Clear the reqs file — nothing was restored, so the final
                # summary must not print a misleading count.
                Set-Content -Path $preservedReqs -Value "" -Encoding utf8
            } else {
                $oldPkgs | Out-File -Encoding utf8 $preservedReqs
                Write-Warn "  Re-adding $extraCount extra packages:"

                # Show each package with its version (from the full freeze).
                $specMap = @{}
                Get-Content $preservedFull | ForEach-Object {
                    $n = Get-PkgName $_
                    if ($n -and -not $specMap.ContainsKey($n)) { $specMap[$n] = $_ }
                }
                foreach ($pkg in $oldPkgs) {
                    if ($specMap.ContainsKey($pkg)) {
                        Write-Dim "    $($specMap[$pkg])"
                    } else {
                        Write-Dim "    $pkg"
                    }
                }

                $prevEAP2 = $ErrorActionPreference
                $ErrorActionPreference = "Continue"
                $pipOutput = & uv pip install --python $newPython @extraIndexArgs -r $preservedReqs 2>&1
                $preserveOk = ($LASTEXITCODE -eq 0)
                $ErrorActionPreference = $prevEAP2
                $pipOutput | Out-File -Append -Encoding utf8 $toolInstallLog
                if ($preserveOk) {
                    Write-Ok "$extraCount packages restored"
                } else {
                    Write-Err "  Error details:"
                    $pipOutput | Select-Object -Last 10 | ForEach-Object { Write-Dim "    $_" }
                }
            }
        }
    }

    # 5. Finalise PATH
    Write-Step "[5/5] Finalising PATH..."

    $localBin   = "$env:USERPROFILE\.local\bin"
    $scriptsDir = "$env:USERPROFILE\.slife\Scripts"

    # Ensure ~/.local/bin is on PATH and remove stale venv entries.
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $newPath  = ($userPath -split ';' | Where-Object { $_ -and $_ -ne $scriptsDir }) -join ';'
    if ($newPath -notlike "*$localBin*") {
        $newPath = "$localBin;$newPath"
    }
    if ($newPath -ne $userPath) {
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        if ($userPath -like "*$scriptsDir*") {
            Write-Dim "  Removed stale PATH entry: $scriptsDir"
        }
    }
    $env:PATH = "$localBin;$env:PATH"

    Write-Ok "slife + credstore commands ready"

    # Verify the binary is actually reachable and show its location.
    $slifeCmd = Get-Command slife -ErrorAction SilentlyContinue
    if ($slifeCmd) {
        Write-Dim "  slife → $($slifeCmd.Source)"
    } else {
        Write-Warn "  slife binary not found on PATH — open a new terminal"
    }

    # Done
    Write-Host ""
    Write-Host "Slife v$version installed successfully!" -ForegroundColor Green
    Write-Host ""
    if ((Test-Path $preservedReqs) -and ((Get-Content $preservedReqs).Count -gt 0)) {
        if ($preserveOk) {
            Write-Host "User-added packages restored:" -ForegroundColor Cyan
            Get-Content $preservedReqs | ForEach-Object { Write-Host "  $([char]0x2713) $_" -ForegroundColor Green }
        } else {
            Write-Warn "Failed to restore user-added packages — run manually:"
            Write-Warn "  uv pip install -r $preservedReqs"
        }
    }
    Write-Host "Get started:" -ForegroundColor Cyan
    Write-Host "  credstore set-password              # set up encrypted backup (first time)"
    Write-Host "  credstore set DEEPSEEK_API_KEY       # store your API key"
    Write-Host "  slife                                # launch the TUI"
    Write-Host ""
    Write-Host "Optional extras:" -ForegroundColor Cyan
    Write-Host "  # Local GGUF embeddings (offline, ~30 MB):"
    $ggufUrl = if ($extraIndexArgs.Count -gt 0) { " $($extraIndexArgs[0]) $($extraIndexArgs[1])" } else { "" }
    Write-Host "  uv pip install --python `"`$(uv tool dir)\slife\Scripts\python.exe`"$ggufUrl `"slife[gguf]`""
    Write-Host "  # With NVIDIA GPU, replace 'cpu' with 'cu121' (CUDA 12.1) in the URL above"
    Write-Host "  # HuggingFace transformer embeddings (~2 GB):"
    Write-Host "  uv pip install --python `"`$(uv tool dir)\slife\Scripts\python.exe`" `"slife[transformer]`""
    Write-Host ""
    Write-Host "More info: $slifeRepo" -ForegroundColor Cyan

} finally {
    if (Test-Path $tmpDir) {
        Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
    }
}
