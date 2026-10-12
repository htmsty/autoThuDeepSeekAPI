# Build autoThuDeepSeekAPI: produces the folder build and the single-file build.
#
# Usage:
#   .\build\build_app.ps1                # both
#   .\build\build_app.ps1 -FolderOnly    # folder build only
#   .\build\build_app.ps1 -OneFileOnly   # single-file build only
#
# IMPORTANT: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 without
# a BOM as ANSI; non-ASCII text (even inside comments) corrupts the parse and the
# build then fails with a confusing PyInstaller usage error.
#
# Layout of the results:
#   dist/autoThuDeepSeekAPI/         folder build (recommended for daily use)
#   dist/autoThuDeepSeekAPI.exe      single-file build
# Both are built into build/_work/_dist first and then copied over, because
# PyInstaller's folder mode also drops a launcher exe in the dist root that has
# no _internal next to it (double-clicking that one always fails).

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

    $spec = Join-Path $root 'build\atd.spec'
    $scratch = Join-Path $root 'build\_work\_dist'
    $finalDist = Join-Path $root 'dist'
    $common = @(
        '--noconfirm',
        '--clean',
        '--workpath', (Join-Path $root 'build\_work'),
        '--distpath', $scratch
    )

    if (-not $OneFileOnly) {
        Write-Host '== building folder version ==' -ForegroundColor Cyan
        $env:ATD_ONEFILE = ''
        $env:ATD_NAME = 'autoThuDeepSeekAPI'
        & $python -m PyInstaller @common $spec
        if ($LASTEXITCODE -ne 0) { throw "folder build failed (exit $LASTEXITCODE)" }

        $folderTarget = Join-Path $finalDist 'autoThuDeepSeekAPI'
        New-Item -ItemType Directory -Force -Path $folderTarget | Out-Null
        Copy-Item -Path (Join-Path $scratch 'autoThuDeepSeekAPI\*') -Destination $folderTarget -Recurse -Force
    }

    if (-not $FolderOnly) {
        Write-Host '== building single-file version ==' -ForegroundColor Cyan
        $env:ATD_ONEFILE = '1'
        $env:ATD_NAME = 'autoThuDeepSeekAPI'
        & $python -m PyInstaller @common $spec
        if ($LASTEXITCODE -ne 0) { throw "single-file build failed (exit $LASTEXITCODE)" }
        Copy-Item -Path (Join-Path $scratch 'autoThuDeepSeekAPI.exe') -Destination (Join-Path $finalDist 'autoThuDeepSeekAPI.exe') -Force
    }

    $env:ATD_ONEFILE = ''
    $env:ATD_NAME = ''

    Write-Host '== done ==' -ForegroundColor Green
    Get-ChildItem -LiteralPath $finalDist | Select-Object Mode, Length, Name | Format-Table -AutoSize
}
finally {
    Pop-Location
}
