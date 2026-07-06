<#
  launch-quickpanel.ps1 - start the Odysseus quick panel (global-hotkey pop-up chat).

  Runs quick_panel.py with the repo venv python, hidden (no console). Idempotent:
  does nothing if a quick-panel process is already running, so it's safe to call
  on every app launch. The panel is a persistent background process with a tray
  icon (Quit from there); it summons on Ctrl+Alt+O.

  Usage:
    powershell -ExecutionPolicy Bypass -File scripts\launch-quickpanel.ps1
#>
# Use python.exe (NOT pythonw.exe): pywebview's EdgeChromium/WinForms backend
# fails to create its window under pythonw. -WindowStyle Hidden keeps the console
# from showing, so it's effectively console-less anyway.
$repo   = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $repo "venv\Scripts\python.exe"
$script = Join-Path $repo "quick_panel.py"

if (-not (Test-Path $venvPy) -or -not (Test-Path $script)) {
    Write-Host "Quick panel: venv python or quick_panel.py missing - skipping." -ForegroundColor Yellow
    return
}

# Already running? (any python hosting quick_panel.py) - do nothing.
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'quick_panel\.py' }
if ($existing) {
    Write-Host "Quick panel already running (PID $($existing.ProcessId -join ', '))." -ForegroundColor Green
    return
}

Start-Process -FilePath $venvPy -ArgumentList $script -WorkingDirectory $repo -WindowStyle Hidden
Write-Host "Quick panel launched (summon with Ctrl+Alt+O)." -ForegroundColor Cyan
