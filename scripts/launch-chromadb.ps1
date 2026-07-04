<#
  launch-chromadb.ps1 - start the ChromaDB vector server that Odysseus connects to.

  Odysseus talks to ChromaDB over HTTP at localhost:8100 (src/chroma_client.py).
  It enables smart per-query tool selection and semantic memory / document RAG.
  Optional: the app runs fine without it (falls back to the full toolset).

  Runs the server (from the isolated C:\Odysseus\chroma-venv) in the background and
  returns. Idempotent: does nothing if something is already listening on the port.

  Usage:
    powershell -ExecutionPolicy Bypass -File scripts\launch-chromadb.ps1
#>
param(
    [int]$Port = 8100,
    [string]$DataDir = "C:\Odysseus\chroma-data",
    [string]$ChromaVenv = "C:\Odysseus\chroma-venv"
)

# Already up? Nothing to do.
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "ChromaDB already listening on port $Port." -ForegroundColor Green
    return
}

$chromaExe = Join-Path $ChromaVenv "Scripts\chroma.exe"
$chromaPy  = Join-Path $ChromaVenv "Scripts\python.exe"
if (-not (Test-Path $chromaPy)) {
    Write-Host "ChromaDB not installed at $ChromaVenv - skipping (run the Odysseus ChromaDB setup first)." -ForegroundColor Yellow
    return
}

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

# Memory discipline: evict cold collection segments under an LRU policy and
# cap the segment cache at 512MB, instead of Chroma's default keep-everything
# behaviour (which grows RAM with every collection touched).
$env:CHROMA_SEGMENT_CACHE_POLICY = 'LRU'
$env:CHROMA_MEMORY_LIMIT_BYTES = '536870912'

# Prefer the chroma.exe CLI; fall back to the module entry point.
if (Test-Path $chromaExe) {
    Start-Process -FilePath $chromaExe -ArgumentList 'run','--host','127.0.0.1','--port',"$Port",'--path',"$DataDir" -WindowStyle Hidden
} else {
    Start-Process -FilePath $chromaPy -ArgumentList '-m','chromadb.cli.cli','run','--host','127.0.0.1','--port',"$Port",'--path',"$DataDir" -WindowStyle Hidden
}

# Wait for the port to come up (Odysseus gates on the same port-open check).
$ok = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 700
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { $ok = $true; break }
}
if ($ok) {
    Write-Host "ChromaDB started on port $Port (data: $DataDir)." -ForegroundColor Green
} else {
    Write-Host "ChromaDB launched but port $Port has not opened yet; it may still be starting." -ForegroundColor Yellow
}
