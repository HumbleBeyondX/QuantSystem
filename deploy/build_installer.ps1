#requires -Version 5.1
<#
.SYNOPSIS
    QuantSystem — End-to-end installer build pipeline.

.DESCRIPTION
    Rebuilds the full desktop app from source:
      1. Frontend  — Next.js static export
      2. Backend   — PyInstaller bundle (qs-backend.exe)
      3. Smoke test — launch qs-backend.exe, hit /health, kill
      4. Installer — electron-builder NSIS installer

    Output:
      desktop\dist\QuantSystem Setup <version>.exe

.EXAMPLE
    PS> .\deploy\build_installer.ps1
    PS> .\deploy\build_installer.ps1 -SkipSmokeTest
    PS> .\deploy\build_installer.ps1 -StopOnError
#>

[CmdletBinding()]
param(
    [switch]$SkipSmokeTest,
    [switch]$StopOnError,
    [int]$SmokeTestPort = 8099
)

$ErrorActionPreference = if ($StopOnError) { "Stop" } else { "Continue" }

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$projectRoot = Split-Path -Parent $scriptDir

function Write-Step {
    param([string]$Message)
    Write-Host "`n==== $Message ====" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [string]$Description,
        [scriptblock]$Block
    )
    Write-Host "  -> $Description" -ForegroundColor White
    & $Block
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne $null) {
        throw "$Description failed (exit code $LASTEXITCODE)"
    }
}

Write-Host "QuantSystem — Production Installer Build" -ForegroundColor Green
Write-Host "Project root: $projectRoot`n" -ForegroundColor Gray

# =============================================================
# Phase 1: Frontend static export
# =============================================================
Write-Step "1/4  Building frontend (Next.js)"

Push-Location (Join-Path $projectRoot "dashboard\frontend")
try {
    if (-not (Test-Path "node_modules")) {
        Invoke-Checked "npm install" { npm install }
    }
    Invoke-Checked "npm run build" { npm run build }

    $outDir = Join-Path $projectRoot "dashboard\frontend\out"
    if (-not (Test-Path (Join-Path $outDir "index.html"))) {
        throw "Frontend build missing: $outDir\index.html not found"
    }
    Write-Host "  OK — static export in $outDir" -ForegroundColor Green
}
finally {
    Pop-Location
}

# =============================================================
# Phase 2: Backend PyInstaller bundle
# =============================================================
Write-Step "2/4  Building backend bundle (PyInstaller)"

Push-Location $projectRoot
try {
    Invoke-Checked "pip install -r deploy/requirements-build.txt" {
        python -m pip install -q -r deploy\requirements-build.txt
    }
    Invoke-Checked "pip install -r deploy/requirements-live.txt" {
        python -m pip install -q -r deploy\requirements-live.txt
    }
    Invoke-Checked "pyinstaller deploy/qs-backend.spec" {
        python -m PyInstaller deploy\qs-backend.spec `
            --clean --noconfirm `
            --distpath deploy\dist `
            --workpath deploy\build
    }

    $exePath = Join-Path $projectRoot "deploy\dist\qs-backend\qs-backend.exe"
    if (-not (Test-Path $exePath)) {
        throw "Backend build missing: $exePath not found"
    }
    $size = (Get-Item $exePath).Length / 1MB
    $dirSize = (Get-ChildItem (Split-Path $exePath) -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB
    Write-Host ("  OK — qs-backend.exe ({0:N1} MB exe, {1:N1} MB total)" -f $size, $dirSize) -ForegroundColor Green
}
finally {
    Pop-Location
}

# =============================================================
# Phase 3: Smoke test — launch the bundled backend and hit /health
# =============================================================
if (-not $SkipSmokeTest) {
    Write-Step "3/4  Smoke testing bundled backend on port $SmokeTestPort"

    $exePath = Join-Path $projectRoot "deploy\dist\qs-backend\qs-backend.exe"
    $testDataDir = Join-Path $projectRoot "deploy\smoke-test-data"
    if (Test-Path $testDataDir) {
        Remove-Item $testDataDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    $env:QS_BACKEND_PORT = $SmokeTestPort
    $env:QS_APP_DATA_DIR = $testDataDir

    $proc = Start-Process -FilePath $exePath -PassThru -WindowStyle Hidden
    try {
        Write-Host "  -> Waiting up to 30s for health endpoint..." -ForegroundColor White
        $ok = $false
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Milliseconds 500
            try {
                $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$SmokeTestPort/health" -TimeoutSec 2 -ErrorAction Stop
                if ($resp.status -eq "healthy") {
                    $ok = $true
                    break
                }
            }
            catch {
                # keep polling
            }
        }

        if (-not $ok) {
            throw "Smoke test failed: backend never reported healthy"
        }

        # Verify a couple of the new endpoints come up
        $reportStatus = Invoke-RestMethod -Uri "http://127.0.0.1:$SmokeTestPort/api/reports/daily/status" -TimeoutSec 5
        $logsList = Invoke-RestMethod -Uri "http://127.0.0.1:$SmokeTestPort/api/logs" -TimeoutSec 5
        Write-Host "  OK — /health healthy, reports scheduler $($reportStatus.running), $(@($logsList).Count) log file(s)" -ForegroundColor Green
    }
    finally {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        # Also catch the uvicorn worker child
        Get-Process -Name "qs-backend" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        if (Test-Path $testDataDir) {
            Remove-Item $testDataDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        $env:QS_BACKEND_PORT = $null
        $env:QS_APP_DATA_DIR = $null
    }
}
else {
    Write-Step "3/4  Smoke test SKIPPED (-SkipSmokeTest)"
}

# =============================================================
# Phase 4: Electron installer
# =============================================================
Write-Step "4/4  Building Electron installer (NSIS)"

Push-Location (Join-Path $projectRoot "desktop")
try {
    if (-not (Test-Path "node_modules")) {
        Invoke-Checked "npm install" { npm install }
    }
    Invoke-Checked "electron-builder --win" { npm run build }

    # electron-builder writes to desktop/dist/
    $installer = Get-ChildItem -Path "dist" -Filter "*Setup*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $installer) {
        throw "Installer not found in desktop\dist\"
    }
    Write-Host "  OK — $($installer.FullName) ($([int]($installer.Length / 1MB)) MB)" -ForegroundColor Green
}
finally {
    Pop-Location
}

# =============================================================
# Done
# =============================================================
Write-Host "`n=============================================" -ForegroundColor Green
Write-Host " BUILD COMPLETE" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
$installer = Get-ChildItem -Path (Join-Path $projectRoot "desktop\dist") -Filter "*Setup*.exe" | Select-Object -First 1
Write-Host " Installer: $($installer.FullName)" -ForegroundColor White
Write-Host " Double-click to install.`n" -ForegroundColor Gray
