#Requires -Version 5.1
<#
  launch-app.ps1 - run Odysseus as a standalone desktop app.

  Starts Ollama, ChromaDB, and the Odysseus server in the BACKGROUND (no console
  windows), waits for the server, then opens Odysseus in its own native application
  window (WebView2 via pywebview; falls back to Edge/Chrome app-mode). Closing the
  window stops the server this launcher started (Ollama/ChromaDB are left running
  as shared background services).

  First run: use launch-windows.ps1 once to create the venv + install deps.
  Then make a desktop shortcut to this file for a one-click app.

  Usage: powershell -ExecutionPolicy Bypass -File .\launch-app.ps1
#>
param([int]$Port = 7000, [string]$BindHost = "127.0.0.1")
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
$venvPyw = Join-Path $PSScriptRoot "venv\Scripts\pythonw.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "venv not found. Run launch-windows.ps1 once first to install dependencies." -ForegroundColor Red
    Read-Host "Press Enter to exit"; exit 1
}

function Start-Helper($relPath) {
    $s = Join-Path $PSScriptRoot $relPath
    if (Test-Path $s) {
        try { & powershell -ExecutionPolicy Bypass -File $s }
        catch { Write-Host ("optional helper failed (" + $relPath + "): " + $_) -ForegroundColor Yellow }
    }
}

# 1. Background services (idempotent - each skips if already up).
Start-Helper "scripts\launch-ollama.ps1"
Start-Helper "scripts\launch-chromadb.ps1"

# 2. Odysseus server (hidden background) - only if it isn't already serving.
$alreadyUp = $false
try { Invoke-WebRequest -Uri "http://${BindHost}:$Port/" -UseBasicParsing -TimeoutSec 2 | Out-Null; $alreadyUp = $true }
catch { if ($_.Exception.Response) { $alreadyUp = $true } }

$serverProc = $null
if (-not $alreadyUp) {
    $serverProc = Start-Process -FilePath $venvPy `
        -ArgumentList '-m', 'uvicorn', 'app:app', '--host', $BindHost, '--port', "$Port" `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
}

# 3. Open the native app window (no console). Blocks until the user closes it.
$env:ODYSSEUS_URL = "http://${BindHost}:$Port"
$winExe = if (Test-Path $venvPyw) { $venvPyw } else { $venvPy }
Start-Process -FilePath $winExe -ArgumentList (Join-Path $PSScriptRoot "app_window.py") -Wait

# 4. On window close: stop the server we started (leave it if it was already up).
if ($serverProc -and -not $serverProc.HasExited) {
    Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue
}
