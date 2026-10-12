# Build autoThuDeepSeekAPI: produces the folder build and the single-file build.
#
# Usage:
#   .\build\build_app.ps1                # both
#   .\build\build_app.ps1 -FolderOnly    # folder build only
#   .\build\build_app.ps1 -OneFileOnly   # single-file build only
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 without a
# BOM as ANSI, which corrupts non-ASCII text and breaks parsing.

param(
    [switch]$FolderOnly,
    [switch]$OneFileOnly
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtualenv interpreter not found: $python (create .venv and install requirements.txt first)"
}

Push-Location $root
try {
    Write-Host '== generating icons ==' -ForegroundColor Cyan
    & $python 'tools\make_icon.py'
    if ($LASTEXITCODE -ne 0) { throw "icon generation failed (exit $LASTEXITCODE)" }

    # Absolute paths: --specpath moves the spec file, so relative datas would resolve wrong.
    $assets = Join-Path $root 'assets'
    $iconFile = Join-Path $assets 'icon.ico'
    $entry = Join-Path $root 'run_dev.py'

    $common = @(
        '--noconfirm',
        '--clean',
        '--windowed',
        '--icon', $iconFile,
        '--add-data', "$assets;assets",
        '--paths', (Join-Path $root 'src'),
        '--workpath', (Join-Path $root 'build\_work'),
        '--specpath', (Join-Path $root 'build\_work'),
        '--distpath', (Join-Path $root 'dist')
    )

    if (-not $OneFileOnly) {
        Write-Host '== building folder version ==' -ForegroundColor Cyan
        & $python -m PyInstaller @common --name 'autoThuDeepSeekAPI' $entry
        if ($LASTEXITCODE -ne 0) { throw "folder build failed (exit $LASTEXITCODE)" }
    }

    if (-not $FolderOnly) {
        Write-Host '== building single-file version ==' -ForegroundColor Cyan
        & $python -m PyInstaller @common --onefile --name 'autoThuDeepSeekAPI-single' $entry
        if ($LASTEXITCODE -ne 0) { throw "single-file build failed (exit $LASTEXITCODE)" }
    }

    Write-Host '== done ==' -ForegroundColor Green
    Get-ChildItem -LiteralPath (Join-Path $root 'dist') | Select-Object Mode, Length, Name | Format-Table -AutoSize
}
finally {
    Pop-Location
}
