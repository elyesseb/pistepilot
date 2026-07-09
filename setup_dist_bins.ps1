Param(
    [string]$TargetDir = (Join-Path $PSScriptRoot "dist\bin"),
    [switch]$UseWinget
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[PistePilot] $Message"
}

function Find-ExecutablePath {
    param([string]$Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        return $command.Source
    }

    $searchPaths = @(
        "$env:ProgramFiles\FFmpeg\bin\$Name",
        "$env:ProgramFiles\FFmpeg\$Name",
        "$env:ProgramFiles\MKVToolNix\$Name",
        "$env:ProgramFiles(x86)\MKVToolNix\$Name",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Packages",
        "$env:ProgramFiles\WindowsApps"
    )

    foreach ($path in $searchPaths) {
        if ([string]::IsNullOrWhiteSpace($path)) {
            continue
        }
        if (Test-Path $path -PathType Leaf) {
            return (Resolve-Path $path).Path
        }
        if (Test-Path $path -PathType Container) {
            $match = Get-ChildItem -Path $path -Recurse -Filter $Name -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($match) {
                return $match.FullName
            }
        }
    }

    return $null
}

function Copy-ExecutableIfFound {
    param(
        [string]$ExecutableName,
        [string]$DestinationDir
    )

    $source = Find-ExecutablePath -Name $ExecutableName
    if (-not $source) {
        return $false
    }

    Copy-Item -LiteralPath $source -Destination (Join-Path $DestinationDir $ExecutableName) -Force
    Write-Step "Copied $ExecutableName from $source"
    return $true
}

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null

$required = @("ffmpeg.exe", "ffprobe.exe", "mkvmerge.exe", "mkvpropedit.exe")

Write-Step "Preparing $TargetDir ..."

foreach ($exe in $required) {
    [void](Copy-ExecutableIfFound -ExecutableName $exe -DestinationDir $TargetDir)
}

$missing = $required | Where-Object { -not (Test-Path (Join-Path $TargetDir $_)) }

if ($missing.Count -gt 0 -and $UseWinget) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Step "Some tools are missing. Trying winget ..."
        if ($missing -contains "ffmpeg.exe" -or $missing -contains "ffprobe.exe") {
            Write-Step "Installing FFmpeg with winget (Gyan.FFmpeg) ..."
            winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
        }
        if ($missing -contains "mkvmerge.exe" -or $missing -contains "mkvpropedit.exe") {
            Write-Step "Installing MKVToolNix with winget (MoritzBunkus.MKVToolNix) ..."
            winget install --id MoritzBunkus.MKVToolNix -e --accept-package-agreements --accept-source-agreements
        }
    }
    else {
        Write-Step "winget is not available. Skipping automatic install."
    }
}

foreach ($exe in $required) {
    if (-not (Test-Path (Join-Path $TargetDir $exe))) {
        [void](Copy-ExecutableIfFound -ExecutableName $exe -DestinationDir $TargetDir)
    }
}

$stillMissing = $required | Where-Object { -not (Test-Path (Join-Path $TargetDir $_)) }

if ($stillMissing.Count -gt 0) {
    Write-Host ""
    Write-Host "Automatic setup failed." -ForegroundColor Yellow
    Write-Host "Please install FFmpeg and MKVToolNix manually, then copy these files into ${TargetDir}:" -ForegroundColor Yellow
    $required | ForEach-Object { Write-Host "- $_" -ForegroundColor Yellow }
    throw "Missing required executables in ${TargetDir}: $($stillMissing -join ', ')"
}

Write-Step "$TargetDir is ready."
Write-Step "Found executables:"
Get-ChildItem -Path $TargetDir -File | Select-Object Name, Length | Format-Table -AutoSize
