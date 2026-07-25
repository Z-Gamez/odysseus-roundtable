<#
  build-app.ps1 - package Odysseus into a standalone Odysseus.exe (PyInstaller).

  Produces a single Odysseus.exe (the native window host) with the Odysseus icon
  embedded. Pin Odysseus.exe to the taskbar for a clean single-button app.

  Run after launch-windows.ps1 has created the venv. Usage:
    powershell -ExecutionPolicy Bypass -File .\build-app.ps1
#>
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Write-Error "venv not found - run launch-windows.ps1 first."; exit 1 }

Write-Host "==> Ensuring PyInstaller is installed" -ForegroundColor Cyan
& $venvPy -m pip install --quiet --upgrade pyinstaller

Write-Host "==> Building Odysseus.exe (this can take a minute)" -ForegroundColor Cyan
& $venvPy -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name Odysseus `
    --icon "static\odysseus.ico" `
    --collect-all webview `
    --collect-all clr_loader `
    --collect-all pythonnet `
    odysseus_app.py
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller build failed - scroll up for the error."; exit 1 }

# The exe must live in the repo root so it can find venv\ and app.py at runtime.
Copy-Item "dist\Odysseus.exe" (Join-Path $PSScriptRoot "Odysseus.exe") -Force
Write-Host ""
Write-Host "Built: $(Join-Path $PSScriptRoot 'Odysseus.exe')" -ForegroundColor Green
Write-Host "Right-click it -> Pin to taskbar for a one-click app." -ForegroundColor Green
