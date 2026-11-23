# Build the Windows one-folder bundle and zip (ADR-005). OBS Studio is never bundled.
#
# Usage (Windows PowerShell 5.1 or PowerShell 7, from any directory):
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 [-Installer]
# Output:
#   dist\captation\                                       captation.exe + captationw.exe + _internal
#   dist\etabli-captation-<version>-windows-x86_64.zip    (+ .zip.sha256)
#   with -Installer and Inno Setup 6.3+ (ISCC.exe):
#   dist\etabli-captation-<version>-windows-x86_64-setup.exe (+ .exe.sha256)
# The builds are unsigned: SmartScreen warns on first start (docs/runbook-windows.md).
param(
    [switch]$Installer
)
$ErrorActionPreference = 'Stop'
Set-Location (Resolve-Path (Join-Path $PSScriptRoot '..\..'))

function Invoke-Checked([string]$what, [scriptblock]$block) {
    & $block
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

function Write-Sha256([string]$file) {
    # .NET resolves relative paths against the process directory, not Set-Location: use full paths.
    $full = (Resolve-Path -LiteralPath $file).Path
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $full).Hash.ToLowerInvariant()
    $line = "$hash  $(Split-Path -Leaf $full)"
    [System.IO.File]::WriteAllText("$full.sha256", "$line`n", [System.Text.Encoding]::ASCII)
}

$version = (Invoke-Checked 'version' {
    uv run python -c "from captation.version import __version__; print(__version__)"
}) | Select-Object -Last 1
$version = "$version".Trim()
$name = "etabli-captation-$version-windows-x86_64"

foreach ($dir in 'build\pyinstaller', 'build\zip', 'dist\captation') {
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
}

Invoke-Checked 'pyinstaller' {
    uv run --group build pyinstaller --noconfirm --clean `
        --distpath dist --workpath build\pyinstaller `
        packaging\windows\captation-windows.spec
}
foreach ($exe in 'captation.exe', 'captationw.exe') {
    if (-not (Test-Path "dist\captation\$exe")) { throw "missing dist\captation\$exe" }
}
Copy-Item LICENSE dist\captation\
Copy-Item DISCLAIMER.md dist\captation\

# Zip with a top-level folder named like the archive.
$stage = "build\zip\$name"
New-Item -ItemType Directory -Force (Split-Path $stage) | Out-Null
Copy-Item -Recurse dist\captation $stage
$zip = "dist\$name.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path $stage -DestinationPath $zip
Write-Sha256 $zip
Write-Host "built $zip"

if ($Installer) {
    $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        $candidates = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
        )
        $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $iscc) {
        Write-Warning 'Inno Setup (ISCC.exe) not found: installer skipped, the zip is complete.'
    } else {
        $source = (Resolve-Path dist\captation).Path
        $out = (Resolve-Path dist).Path
        Invoke-Checked 'ISCC' {
            & $iscc "/DAppVersion=$version" "/DSourceDir=$source" "/DOutputDir=$out" `
                "/DOutputBaseName=$name-setup" packaging\windows\etabli-captation.iss
        }
        Write-Sha256 "dist\$name-setup.exe"
        Write-Host "built dist\$name-setup.exe"
    }
}
