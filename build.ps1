# Builds the standalone overlay (dist\AACMapOverlay\AACMapOverlay.exe) and,
# when Inno Setup 6 is installed, the installer (dist\AACMapOverlay-Setup.exe).
# Used by the GitHub release workflow too.
#   powershell -ExecutionPolicy Bypass -File build.ps1 [-Python path\to\python.exe]
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$version = (Select-String -Path version.py -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Building AAC Map Overlay $version"

& $Python -m PyInstaller --noconfirm --clean --windowed `
  --name AACMapOverlay `
  --icon assets\icon.ico `
  --add-data "ui.html;." `
  --add-data "page_tools.js;." `
  --add-data "dps.html;." `
  --add-data "assets\icon.ico;assets" `
  --collect-submodules winrt `
  --collect-all webview `
  --collect-all adblock `
  overlay.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$iscc = @(
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
  & $iscc "/DAppVersion=$version" installer.iss
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
  Write-Host "Installer: dist\AACMapOverlay-Setup.exe"
} else {
  Write-Host "Inno Setup 6 not found: skipped the installer (the app is in dist\AACMapOverlay)."
}
