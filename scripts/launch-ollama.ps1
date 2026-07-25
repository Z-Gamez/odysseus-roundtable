<#
  launch-ollama.ps1 - start the Ollama API server in the background if it isn't running.

  Local models (chat + the Round Table's local roles) need Ollama listening on
  127.0.0.1:11434. Idempotent: does nothing if something is already on the port,
  so it's safe to call on every launch (and alongside the Ollama tray app).

  Usage: powershell -ExecutionPolicy Bypass -File scripts\launch-ollama.ps1
#>
param([int]$Port = 11434)

if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Ollama already listening on port $Port." -ForegroundColor Green
    return
}

$ollama = $null
$cmd = Get-Command ollama -ErrorAction SilentlyContinue
if ($cmd) { $ollama = $cmd.Source }
if (-not $ollama) {
    foreach ($p in @(
        "$env:LocalAppData\Programs\Ollama\ollama.exe",
        "$env:ProgramFiles\Ollama\ollama.exe",
        "${env:ProgramFiles(x86)}\Ollama\ollama.exe"
    )) { if ($p -and (Test-Path $p)) { $ollama = $p; break } }
}
if (-not $ollama) {
    Write-Host "Ollama not found - skipping (install from https://ollama.com for local models)." -ForegroundColor Yellow
    return
}

# Performance: flash attention speeds up attention and cuts KV-cache memory. Only
# applies when WE start Ollama here - an already-running Ollama (tray app) won't pick
# it up until it is restarted.
if (-not $env:OLLAMA_FLASH_ATTENTION) { $env:OLLAMA_FLASH_ATTENTION = '1' }
Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 700
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { $ok = $true; break }
}
if ($ok) { Write-Host "Ollama started on port $Port." -ForegroundColor Green }
else { Write-Host "Ollama launched but port $Port not open yet; it may still be starting." -ForegroundColor Yellow }
