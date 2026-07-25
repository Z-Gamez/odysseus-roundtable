<#
  launch-chrome-debug.ps1 — start Chrome so Odysseus's `browser` tool can drive it.

  Odysseus attaches to Chrome over the DevTools protocol (CDP). Chrome only exposes
  that when started with --remote-debugging-port — and since Chrome 136 that flag is
  silently IGNORED for your default profile folder, so driving your everyday Chrome
  directly is impossible.

  Default mode therefore uses a CLONE of your real profile: your logins, cookies,
  bookmarks and extensions are mirrored (caches excluded) into "OdysseusChrome" and
  re-synced on every launch, then Chrome starts from the clone with the debug port.
  It runs alongside your normal Chrome. Changes you make in normal Chrome show up
  in the agent's browser on its next launch.

  -Automation: use the old blank "OdysseusAutomation" profile instead (no personal
  data exposed to the agent — log into sites manually there as needed).

  Usage:
    powershell -ExecutionPolicy Bypass -File scripts\launch-chrome-debug.ps1
    powershell -ExecutionPolicy Bypass -File scripts\launch-chrome-debug.ps1 -Automation
#>
param([int]$Port = 9222, [switch]$Automation)

$chrome = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $chrome) { Write-Error "Chrome not found. Install Google Chrome and retry."; exit 1 }

# Already listening? Nothing to do.
try {
  $null = Invoke-WebRequest -Uri "http://localhost:$Port/json/version" -UseBasicParsing -TimeoutSec 2
  Write-Host "Chrome is already listening for automation on port $Port." -ForegroundColor Green
  return
} catch {}

if ($Automation) {
  $udd = "$env:LOCALAPPDATA\Google\Chrome\OdysseusAutomation"
  Write-Host "Using the blank automation profile: $udd" -ForegroundColor Cyan
} else {
  $src = "$env:LOCALAPPDATA\Google\Chrome\User Data"
  $udd = "$env:LOCALAPPDATA\Google\Chrome\OdysseusChrome"
  if (-not (Test-Path "$udd\Local State")) {
    # First run: seed the clone from the real profile (bookmarks/history/
    # extensions come through; Chrome's app-bound encryption means COOKIES
    # cannot survive a copy — sign into sites once in the clone, it persists).
    Write-Host "Seeding the agent browser from your Chrome profile (one-time)..." -ForegroundColor Cyan
    robocopy $src $udd /E /R:0 /W:0 /MT:8 /NFL /NDL /NJH /NJS /NP `
      /XF lockfile *.tmp `
      /XD Cache "Code Cache" GPUCache GrShaderCache ShaderCache DawnGraphiteCache `
          DawnWebGPUCache "Media Cache" "Service Worker" Crashpad CrashpadMetrics `
          Snapshots component_crx_cache extensions_crx_cache OptimizationGuidePredictionModels | Out-Null
    if ($LASTEXITCODE -ge 8) {
      Write-Host "Profile seed reported problems (robocopy exit $LASTEXITCODE) - continuing with what copied." -ForegroundColor Yellow
    }
  } else {
    # Persistent clone — only refresh bookmarks so logins made in it survive.
    try { Copy-Item "$src\Default\Bookmarks" "$udd\Default\Bookmarks" -Force -ErrorAction SilentlyContinue } catch {}
  }
  if (-not (Test-Path "$udd\Local State")) {
    Write-Error "Profile clone is missing 'Local State' - cannot launch. Is Chrome installed with a profile at `"$src`"?"
    exit 1
  }
  Write-Host "Agent browser profile ready: $udd" -ForegroundColor Cyan
}

& $chrome "--remote-debugging-port=$Port" "--user-data-dir=$udd" "--start-maximized" "about:blank"
Start-Sleep -Milliseconds 1500
try {
  $r = Invoke-WebRequest -Uri "http://localhost:$Port/json/version" -UseBasicParsing -TimeoutSec 4
  Write-Host "OK - Chrome is listening for automation on port $Port." -ForegroundColor Green
  Write-Host "In Odysseus, the agent's browser tool will connect automatically (browser_cdp_url = http://localhost:$Port)."
} catch {
  Write-Host "Chrome launched but port $Port isn't responding yet; give it a few seconds." -ForegroundColor Yellow
}
