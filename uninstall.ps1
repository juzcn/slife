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

Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║        Slife Uninstaller            ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

$installed = uv tool list 2>&1 | Select-String "slife"
if ($installed) {
    Write-Host "Uninstalling slife (slife + credstore share the same venv)..." -ForegroundColor Yellow
    uv tool uninstall slife *>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [OK] slife + credstore removed" -ForegroundColor Green
    } else {
        Write-Host "  [!!] uninstall failed" -ForegroundColor Red
    }
} else {
    Write-Host "slife is not installed." -ForegroundColor DarkGray
}

# ── Clean up wrapper binaries ───────────────────────────────────────────
$localBin = "$env:USERPROFILE\.local\bin"
foreach ($bin in @("$localBin\slife.exe", "$localBin\slife.cmd", "$localBin\credstore.exe", "$localBin\credstore.cmd")) {
    if (Test-Path $bin) {
        Remove-Item $bin -Force -ErrorAction SilentlyContinue
        Write-Host "  Removed: $bin" -ForegroundColor DarkGray
    }
}

# ── Remaining data ─────────────────────────────────────────────────────
Write-Host ""
$dataDir = "$env:USERPROFILE\.slife"

$remain = @()
if (Test-Path $dataDir) {
    $size = "{0:F1} MB" -f ((Get-ChildItem $dataDir -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB)
    $remain += "  ~\.slife\           ($size) — config, logs, databases, skills"
}

if ($remain.Count -gt 0) {
    Write-Host "Data files NOT removed (delete manually if desired):" -ForegroundColor Yellow
    foreach ($r in $remain) {
        Write-Host $r -ForegroundColor DarkGray
    }
} else {
    Write-Host "No remaining data files." -ForegroundColor Green
}

Write-Host ""
Write-Host "Done." -ForegroundColor Cyan
