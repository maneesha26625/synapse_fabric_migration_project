# Starts the Migration Accelerator: the API (port 8001) and the UI dev server (port 5173), then opens the UI.
# Usage:  .\start-ui.ps1
#
# Run it once. After that you never restart anything by hand:
#   * the API runs with --reload: it restarts itself within a second when backend code changes;
#   * the UI dev server reloads the page itself when frontend code changes;
#   * the last discovery and the Fabric target come back after every restart (sign in to Synapse again only
#     to run discovery again or load table data).
# Running it again is safe: whatever is already running is reused, never started twice.
$root = $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "src"

function Test-Port([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# Prefer the project venv if it works on this machine, else a system Python.
$python = $null
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
foreach ($candidate in @($venvPy, "py", "python")) {
    try { & $candidate -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $python = $candidate; break } } catch {}
}
if (-not $python) { Write-Error "No working Python found."; exit 1 }

if (Test-Port 8001) {
    Write-Host "API already running on port 8001: reusing it."
} elseif (Get-ScheduledTask -TaskName "MigrationAcceleratorAPI" -ErrorAction SilentlyContinue) {
    # Installed as a background task (install-api-service.ps1): start that rather than a second, window-bound copy.
    Write-Host "Starting the background API task."
    Start-ScheduledTask -TaskName "MigrationAcceleratorAPI"
    Start-Sleep 3
} else {
    Write-Host "Starting API (auto-reload) with: $python"
    # Its own window, so its log stays visible and closing this one does not stop it.
    Start-Process -FilePath $python -ArgumentList "-m", "discovery_agent.api", "--port", "8001", "--reload" -WorkingDirectory $root
    Start-Sleep 2
}

Push-Location (Join-Path $root "frontend")
if (-not (Test-Path node_modules)) { npm install --no-audit --no-fund }
if (Test-Port 5173) {
    Write-Host "UI dev server already running on port 5173: opening it."
    Start-Process "http://localhost:5173"
} else {
    Start-Process "http://localhost:5173"
    npm run dev
}
Pop-Location
