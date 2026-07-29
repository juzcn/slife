<#
.SYNOPSIS
    Slife uninstaller for Windows PowerShell.

.DESCRIPTION
    Uninstalls slife (and credstore, same venv) from uv tool install.
    User data (~\.slife\) is NOT removed — delete it manually if needed.

.EXAMPLE
    .\uninstall.ps1
#>

$ErrorActionPreference = "Continue"

# ── Helpers (same as install.ps1) ────────────────────────────────────
function Write-Step($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Ok($msg)   { Write-Host "  $([char]0x2713) $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "  $([char]0x2717) $msg" -ForegroundColor Red }
function Write-Dim($msg)  { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Warn($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Box($msg)  { Write-Host $msg -ForegroundColor Cyan }

Write-Host ""
Write-Box "╔══════════════════════════════════════╗"
Write-Box "║        Slife Uninstaller            ║"
Write-Box "╚══════════════════════════════════════╝"
Write-Host ""

# ── 1. Uninstall from uv tool ───────────────────────────────────────
$installed = uv tool list 2>$null | Select-String "slife"
if ($installed) {
    Write-Step "Uninstalling slife (slife + credstore share the same venv)..."

    # Capture stderr separately so we can show it on failure.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $uninstallOutput = uv tool uninstall slife 2>&1
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEAP

    if ($ok) {
        Write-Ok "slife + credstore removed"
    } else {
        Write-Fail "uninstall failed"
        if ($uninstallOutput) {
            $uninstallOutput | Select-Object -Last 5 | ForEach-Object { Write-Dim "    $_" }
        }
    }
} else {
    Write-Dim "slife is not installed."
}

# ── 2. Clean up wrapper binaries ─────────────────────────────────────
$localBin = "$env:USERPROFILE\.local\bin"
foreach ($bin in @("$localBin\slife.exe", "$localBin\slife.cmd",
                   "$localBin\credstore.exe", "$localBin\credstore.cmd")) {
    if (Test-Path $bin) {
        Remove-Item $bin -Force -ErrorAction SilentlyContinue
        Write-Dim "  Removed: $bin"
    }
}

# ── 3. Remaining data ────────────────────────────────────────────────
Write-Host ""
$dataDir = "$env:USERPROFILE\.slife"

$remain = @()
if (Test-Path $dataDir) {
    $size = "{0:F1} MB" -f ((Get-ChildItem $dataDir -Recurse -ErrorAction SilentlyContinue |
        Measure-Object Length -Sum).Sum / 1MB)
    $remain += "  ~\.slife\           ($size) — config, logs, databases, skills"
}
$credstoreDir = "$env:USERPROFILE\.credstore"
if (Test-Path $credstoreDir) {
    $remain += "  ~\.credstore\       — encrypted credential backup"
}

if ($remain.Count -gt 0) {
    Write-Warn "Data files NOT removed (delete manually if desired):"
    foreach ($r in $remain) {
        Write-Dim $r
    }
} else {
    Write-Ok "No remaining data files."
}

Write-Host ""
Write-Box "Done."
