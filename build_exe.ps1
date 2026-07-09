Param(
    [string]$Python = "python",
    [switch]$SetupBins,
    [switch]$GuiOnly,
    [switch]$Release
)

$ErrorActionPreference = "Stop"

$distDir = Join-Path $PSScriptRoot "dist"
$releaseDir = Join-Path $PSScriptRoot "release"
$setupScript = Join-Path $PSScriptRoot "setup_dist_bins.ps1"

function Ensure-Dir {
    param([string]$Path)
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Clear-BuildTarget {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    }
}

Get-Process -Name "PistePilot-GUI", "PistePilot-CLI", "PistePilot" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

Ensure-Dir $distDir
Ensure-Dir (Join-Path $distDir "bin")
Ensure-Dir (Join-Path $distDir "logs")
Clear-BuildTarget (Join-Path $distDir "PistePilot-CLI.exe")
Clear-BuildTarget (Join-Path $distDir "PistePilot-GUI.exe")

if (-not $GuiOnly -and -not $Release) {
    & $Python -m PyInstaller `
        --clean `
        --noconfirm `
        --onefile `
        --name PistePilot-CLI `
        --collect-all questionary `
        --hidden-import tkinter `
        --hidden-import questionary `
        --hidden-import rich `
        pistepilot/cli.py
}

& $Python -m PyInstaller `
    --clean `
    --noconfirm `
    --onefile `
    --windowed `
    --name PistePilot-GUI `
    --hidden-import tkinter `
    --hidden-import rich `
    pistepilot/gui.py

if ($Release) {
    if (Test-Path -LiteralPath $releaseDir) {
        Remove-Item -Recurse -Force $releaseDir
    }
    Ensure-Dir $releaseDir
    Ensure-Dir (Join-Path $releaseDir "bin")
    Copy-Item -LiteralPath (Join-Path $distDir "PistePilot-GUI.exe") -Destination (Join-Path $releaseDir "PistePilot.exe") -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "README.md") -Destination (Join-Path $releaseDir "README.md") -Force

    $licensePath = Join-Path $PSScriptRoot "LICENSE"
    if (Test-Path -LiteralPath $licensePath) {
        Copy-Item -LiteralPath $licensePath -Destination (Join-Path $releaseDir "LICENSE") -Force
    }

    if ($SetupBins) {
        & $setupScript -TargetDir (Join-Path $releaseDir "bin")
    }
}
elseif ($SetupBins) {
    & $setupScript -TargetDir (Join-Path $distDir "bin")
}

if ($Release) {
    Write-Host "Release build complete. End-user package available in release/PistePilot.exe"
}
elseif ($GuiOnly) {
    Write-Host "GUI-only build complete. Executable available in dist/PistePilot-GUI.exe"
}
else {
    Write-Host "Build complete. Executables are available in dist/PistePilot-CLI.exe and dist/PistePilot-GUI.exe"
}
