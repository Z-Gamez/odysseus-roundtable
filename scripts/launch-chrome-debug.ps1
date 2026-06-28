<#
  launch-chrome-debug.ps1 — start Chrome so Odysseus's `browser` tool can drive it.

  Odysseus attaches to Chrome over the DevTools protocol (CDP). Chrome only exposes
  that when started with --remote-debugging-port, so run this once before asking the
  agent to use the browser.

  Default: a dedicated "OdysseusAutomation" profile that runs ALONGSIDE your normal
  Chrome. Log into the sites you want the agent to use once; the profile remembers them.

  -RealProfile: use your everyday Chrome profile (existing logins/tabs). For this the
  debug port only enables if Chrome is FULLY closed first — quit Chrome, then run this.

  Usage:
    powershell -ExecutionPolicy Bypass -File scripts\launch-chrome-debug.ps1
    powershell -ExecutionPolicy Bypass -File scripts\launch-chrome-debug.ps1 -RealProfile
#>
param([int]$Port = 9222, [switch]$RealProfile)

$chrome = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $chrome) { Write-Error "Chrome not found. Install Google Chrome and retry."; exit 1 }

if ($RealProfile) {
  $udd = "$env:LOCALAPPDATA\Google\Chrome\User Data"
  Write-Host "Using your REAL Chrome profile. If Chrome is already open, fully quit it first or the debug port will NOT enable." -ForegroundColor Yellow
} else {
  $udd = "$env:LOCALAPPDATA\Google\Chrome\OdysseusAutomation"
  Write-Host "Using a dedicated automation profile: $udd" -ForegroundColor Cyan
  Write-Host "Log into sites once here; it remembers. Runs alongside your normal Chrome." -ForegroundColor Cyan
}

& $chrome "--remote-debugging-port=$Port" "--user-data-dir=$udd" "about:blank"
Start-Sleep -Milliseconds 1200
try {
  $r = Invoke-WebRequest -Uri "http://localhost:$Port/json/version" -UseBasicParsing -TimeoutSec 4
  Write-Host "OK - Chrome is listening for automation on port $Port." -ForegroundColor Green
  Write-Host "In Odysseus, the agent's browser tool will connect automatically (browser_cdp_url = http://localhost:$Port)."
} catch {
  Write-Host "Chrome launched but port $Port isn't responding yet. If you used -RealProfile, make sure Chrome was fully closed first." -ForegroundColor Yellow
}
