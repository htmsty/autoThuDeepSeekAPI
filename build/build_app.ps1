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

    $spec = Join-Path $root 'build\atd.spec'
    $common = @(
        '--noconfirm',
        '--clean',
        '--workpath', (Join-Path $root 'build\_work'),
        '--distpath', (Join-Path $root 'dist')
    )

    if (-not $OneFileOnly) {
        Write-Host '== building folder version ==' -ForegroundColor Cyan
        $env:ATD_ONEFILE = ''
        $env:ATD_NAME = 'autoThuDeepSeekAPI'
        & $python -m PyInstaller @common $spec
        if ($LASTEXITCODE -ne 0) { throw "folder build failed (exit $LASTEXITCODE)" }

        # PyInstaller 会把启动器 exe 留在 dist 根目录一份（没有配套的 _internal，
        # 双击必失败）。这里把它删掉，避免和单文件版混淆。
        $stray = Join-Path $root 'dist\autoThuDeepSeekAPI.exe'
        $inside = Join-Path $root 'dist\autoThuDeepSeekAPI\autoThuDeepSeekAPI.exe'
        if ((Test-Path -LiteralPath $stray -PathType Leaf) -and (Test-Path -LiteralPath $inside -PathType Leaf)) {
            Remove-Item -LiteralPath $stray -Force
            Write-Host "removed stray launcher: $stray"
        }
    }

    if (-not $FolderOnly) {
        Write-Host '== building single-file version ==' -ForegroundColor Cyan
        $env:ATD_ONEFILE = '1'
        $env:ATD_NAME = 'autoThuDeepSeekAPI-single'
        & $python -m PyInstaller @common $spec
        if ($LASTEXITCODE -ne 0) { throw "single-file build failed (exit $LASTEXITCODE)" }
    }
    $env:ATD_ONEFILE = ''
    $env:ATD_NAME = ''

    Write-Host '== done ==' -ForegroundColor Green
    Get-ChildItem -LiteralPath (Join-Path $root 'dist') | Select-Object Mode, Length, Name | Format-Table -AutoSize
}
finally {
    Pop-Location
}
