# Starts the API (port 8001) and the UI dev server (port 5173), then opens the UI.
# Usage:  .\start-ui.ps1
#
# Safe to run again at any time; nothing ever has to be restarted by hand. The API restarts
# by itself when its code changes, and keeps its sign-ins, discovery and migration run across
# restarts. The UI dev server reloads by itself too. So:
#   * an API already running that updates itself is reused;
#   * an older one that does not (started before it could) is stopped and replaced, once;
#   * a UI dev server already running is reused.
$root = $PSScriptRoot
$apiPort = 8001
$uiPort = 5173
$env:PYTHONPATH = Join-Path $root "src"

function Get-ApiHealth {
    try { Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/api/health" -TimeoutSec 3 -UseBasicParsing } catch { $null }
}

function Get-Listeners([int]$port) {
    @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
}

# -- the API -----------------------------------------------------------------------------
$health = Get-ApiHealth
if ($health -and $health.supervised) {
    Write-Host "The API is already running on port $apiPort and updates itself: reusing it."
} else {
    foreach ($procId in Get-Listeners $apiPort) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        if ($health -and $proc -and $proc.CommandLine -match "discovery_agent\.api|discovery-agent-api") {
            Write-Host "Stopping the old API (process $procId): it does not update itself, so this one replaces it."
            Stop-Process -Id $procId -Force
        } else {
            Write-Error "Port $apiPort is used by another program ($($proc.Name), process $procId). Stop it first."
            exit 1
        }
    }
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Listeners $apiPort).Count -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }

    # Prefer the project venv if it works on this machine, else a system Python.
    $python = $null
    $venvPy = Join-Path $root ".venv\Scripts\python.exe"
    foreach ($candidate in @($venvPy, "py", "python")) {
        try { & $candidate -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $python = $candidate; break } } catch {}
    }
    if (-not $python) { Write-Error "No working Python found."; exit 1 }

    Write-Host "Starting the API with: $python (it restarts by itself when the code changes)"
    Start-Process -FilePath $python -ArgumentList "-m", "discovery_agent.api", "--port", "$apiPort" -WorkingDirectory $root
    # It may first take back the last session (sign-ins, discovery, run): wait until it answers.
    $deadline = (Get-Date).AddSeconds(90)
    while (-not (Get-ApiHealth)) {
        if ((Get-Date) -gt $deadline) { Write-Warning "The API has not answered yet: see its window."; break }
        Start-Sleep -Milliseconds 500
    }
}

# -- the UI ------------------------------------------------------------------------------
if ((Get-Listeners $uiPort).Count) {
    Write-Host "The UI is already running on port $uiPort (it reloads by itself): opening it."
    Start-Process "http://localhost:$uiPort"
    exit 0
}
Push-Location (Join-Path $root "frontend")
if (-not (Test-Path node_modules)) { npm install --no-audit --no-fund }
Start-Process "http://localhost:$uiPort"
npm run dev
Pop-Location
