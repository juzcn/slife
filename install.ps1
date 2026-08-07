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
    Write-Host "bun               : auto-install bun if needed (required for nvidia-nim MCP)"
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
    #
    # npx is bundled with npm >= 5.2 (shipped with every Node.js install
    # since 2017).  We use content-matching instead of $LASTEXITCODE
    # because PowerShell 5.1 sometimes mishandles exit codes from .cmd
    # batch files.
    Write-Step "[2/5] Ensuring npx (Node.js) is available..."

    # Try to run npx (or npm) and extract a version string.
    # Returns the version on success, or $null.
    function Test-NpxAvailable {
        # 1) npx already on PATH
        try {
            $out = npx --version 2>&1 | ForEach-Object { "$_" }
            $ver = ($out -join "`n").Trim()
            if ($ver -match '^\d+\.') { return $ver }
        } catch { }

        # 2) npm works — npx is always alongside it; if npm runs,
        #    npx.cmd is in the same directory and will also work
        try {
            $out = npm --version 2>&1 | ForEach-Object { "$_" }
            $ver = ($out -join "`n").Trim()
            if ($ver -match '^\d+\.') { return "npx v$ver (via npm)" }
        } catch { }

        return $null
    }

    function Find-NodeDirs {
        $dirs = @()
        # Common install locations
        foreach ($candidate in @(
            "$env:ProgramFiles\nodejs",
            "${env:ProgramFiles(x86)}\nodejs",
            "$env:LOCALAPPDATA\fnm\node-versions",
            "$env:APPDATA\nvm",
            "$env:USERPROFILE\nvm"
        )) {
            if (Test-Path (Join-Path $candidate "npm.cmd")) {
                $dirs += $candidate
            }
        }
        # Follow the 'node' command if it's on PATH but npm isn't
        if (-not $dirs -and (Get-Command node -ErrorAction SilentlyContinue)) {
            $nodeDir = Split-Path (Get-Command node).Source -Parent
            if (Test-Path (Join-Path $nodeDir "npm.cmd")) {
                $dirs += $nodeDir
                Write-Dim "Found Node.js via 'node' command at $nodeDir"
            }
        }
        return $dirs
    }

    function Try-AddNpxFromDirs($dirs) {
        foreach ($d in $dirs) {
            $env:PATH = "$d;$env:PATH"
            try {
                $out = npx --version 2>&1 | ForEach-Object { "$_" }
                $ver = ($out -join "`n").Trim()
                if ($ver -match '^\d+\.') {
                    Write-Ok "npx v$ver (found at $d)"
                    return $ver
                }
            } catch { }
            # npx.cmd may be broken — try the underlying npm.cmd instead
            try {
                $npmExe = Join-Path $d "npm.cmd"
                $out = & $npmExe --version 2>&1 | ForEach-Object { "$_" }
                $ver = ($out -join "`n").Trim()
                if ($ver -match '^\d+\.') {
                    Write-Ok "npx v$ver (via npm at $d)"
                    return $ver
                }
            } catch { }
        }
        return $null
    }

    # --- main flow ---
    $haveNpx = $false

    # 1) Quick check: npx already functional on PATH
    $ver = Test-NpxAvailable
    if ($ver) {
        Write-Ok $ver
        $haveNpx = $true
    }

    # 2) Scan known install directories
    if (-not $haveNpx) {
        $foundDirs = Find-NodeDirs
        $ver = Try-AddNpxFromDirs $foundDirs
        if ($ver) { $haveNpx = $true }
    }

    # 3) Not found — try winget install
    if (-not $haveNpx) {
        Write-Dim "npx not found, installing Node.js..."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Dim "Installing Node.js LTS via winget..."
            winget install OpenJS.NodeJS.LTS --source winget --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) {
                Write-Dim "winget install returned exit code $LASTEXITCODE (may already be installed)"
            }
            # Refresh PATH from registry then re-scan
            $env:PATH = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                        [System.Environment]::GetEnvironmentVariable("Path", "User")
            $ver = Test-NpxAvailable
            if ($ver) {
                Write-Ok $ver
                $haveNpx = $true
            }
        }
    }

    # 4) Final attempt: re-scan all directories (winget may have populated one)
    if (-not $haveNpx) {
        $foundDirs = Find-NodeDirs
        $ver = Try-AddNpxFromDirs $foundDirs
        if ($ver) { $haveNpx = $true }
    }

    if (-not $haveNpx) {
        Write-Warn "WARNING: npx not available."
        Write-Warn "  These MCP servers require npx and will NOT work:"
        Write-Warn "    file-search, serper, tavily-mcp, github, amap-maps, filesystem"
        Write-Warn "  Install Node.js LTS from https://nodejs.org then re-run this installer."
        Write-Warn "Help: $slifeRepo"
        exit 1
    }

    # -- bun / bunx (required by nvidia-nim MCP server) --
    Write-Step "[2b] Ensuring bun (JavaScript runtime) is available..."

    function Test-BunAvailable {
        try {
            $out = bun --version 2>&1 | ForEach-Object { "$_" }
            $ver = ($out -join "`n").Trim()
            if ($ver -match '^\d+\.') { return $ver }
        } catch { }
        return $null
    }

    $haveBun = $false
    $bunVer = Test-BunAvailable
    if ($bunVer) {
        Write-Ok "bun v$bunVer"
        $haveBun = $true
    }

    if (-not $haveBun) {
        Write-Dim "bun not found, installing..."
        if (Get-Command powershell -ErrorAction SilentlyContinue) {
            powershell -ExecutionPolicy ByPass -c "irm bun.sh/install.ps1 | iex"
            $env:PATH = "$env:USERPROFILE\.bun\bin;$env:PATH"
            $bunVer = Test-BunAvailable
            if ($bunVer) {
                Write-Ok "bun v$bunVer"
                $haveBun = $true
            }
        }
    }

    if (-not $haveBun) {
        Write-Warn "WARNING: bun not available."
        Write-Warn "  The nvidia-nim MCP server requires bunx and will NOT work."
        Write-Warn "  Install bun manually from https://bun.sh then re-run this installer."
        Write-Warn "  All other MCP servers (npx-based) are unaffected."
    }

    # Optional: Mosquitto MQTT broker
    Write-Step "[optional] Checking Mosquitto (MQTT broker for multi-agent mesh)..."

    function Find-MosquittoDir {
        # 1) Already on PATH
        if (Get-Command mosquitto -ErrorAction SilentlyContinue) { return "" }
        # 2) Common install directories
        foreach ($candidate in @(
            "${env:ProgramFiles}\Mosquitto",
            "${env:ProgramFiles(x86)}\Mosquitto"
        )) {
            if (Test-Path (Join-Path $candidate "mosquitto.exe")) { return $candidate }
        }
        # 3) Registry uninstall entries (handles custom install locations)
        foreach ($regPath in @(
            "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
            "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
            "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*"
        )) {
            try {
                $entry = Get-ItemProperty -Path $regPath -ErrorAction SilentlyContinue |
                    Where-Object { $_.DisplayName -match 'mosquitto' } |
                    Select-Object -First 1
                if ($entry) {
                    foreach ($prop in @('UninstallString', 'DisplayIcon')) {
                        if ($entry.$prop) {
                            $dir = Split-Path $entry.$prop.Trim('"') -Parent
                            if (Test-Path (Join-Path $dir "mosquitto.exe")) { return $dir }
                        }
                    }
                }
            } catch { }
        }
        return $null
    }

    $mosqDir = Find-MosquittoDir
    if ($mosqDir -ne $null) {
        if ($mosqDir -ne "") { $env:PATH = "$mosqDir;$env:PATH" }
        Write-Ok "mosquitto found"
    } else {
        Write-Warn "  Mosquitto not found."
        Write-Dim "  Required for: A2A multi-agent mesh communication"
        Write-Dim "  Without it:  slife works normally, just without P2P agent features"
        try { $choice = Read-Host "  Install Mosquitto? (y/n, default: n)" } catch { $choice = "n" }
        if ($choice -eq 'y' -or $choice -eq 'Y') {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                Write-Dim "Installing Mosquitto via winget..."
                winget install EclipseFoundation.Mosquitto --source winget --accept-package-agreements --accept-source-agreements
                # winget returns non-zero when the package is already installed
                # and no upgrade is available — refresh PATH and re-scan.
                $env:PATH = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                            [System.Environment]::GetEnvironmentVariable("Path", "User")
                $mosqDir = Find-MosquittoDir
                if ($mosqDir -ne $null) {
                    if ($mosqDir -ne "") { $env:PATH = "$mosqDir;$env:PATH" }
                    Write-Ok "Mosquitto ready"
                    Write-Host "  To start Mosquitto:" -ForegroundColor Cyan
                    Write-Host "    net start mosquitto"
                    Write-Host "  Or run manually:"
                    Write-Host "    mosquitto -d -p 1883"
                } else {
                    Write-Warn "  Mosquitto not found after install. Install manually:"
                    Write-Warn "    https://mosquitto.org/download/"
                }
            } else {
                Write-Warn "  No supported package manager found (winget not available)."
                Write-Warn "  Install manually: https://mosquitto.org/download/"
            }
        } else {
            Write-Dim "  Skipped. Install later with: winget install EclipseFoundation.Mosquitto --source winget"
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
            Write-Dim "Found previous installation — will preserve user-added packages..."

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
    $hasExtras  = $false
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
                $hasExtras  = $false
            } else {
                $hasExtras = $true
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
    $bunBin     = "$env:USERPROFILE\.bun\bin"
    $scriptsDir = "$env:USERPROFILE\.slife\Scripts"

    # Ensure ~/.local/bin and ~/.bun/bin are on PATH; remove stale venv entries.
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $newPath  = ($userPath -split ';' | Where-Object { $_ -and $_ -ne $scriptsDir }) -join ';'
    if ($newPath -notlike "*$localBin*") {
        $newPath = "$localBin;$newPath"
    }
    if ($newPath -notlike "*$bunBin*") {
        $newPath = "$bunBin;$newPath"
    }
    if ($newPath -ne $userPath) {
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        if ($userPath -like "*$scriptsDir*") {
            Write-Dim "  Removed stale PATH entry: $scriptsDir"
        }
    }
    $env:PATH = "$bunBin;$localBin;$env:PATH"

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
    if ($hasExtras) {
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
    Write-Host "  uv pip install --python `"`$(uv tool dir)\slife\Scripts\python.exe`"$ggufUrl llama-cpp-python"
    Write-Host "  # HuggingFace transformer embeddings (~2 GB):"
    Write-Host "  uv pip install --python `"`$(uv tool dir)\slife\Scripts\python.exe`" sentence-transformers"
    Write-Host ""
    Write-Host "More info: $slifeRepo" -ForegroundColor Cyan

} finally {
    if (Test-Path $tmpDir) {
        Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
    }
}
