# Starts the API (port 8000) and the UI dev server (port 5173), then opens the UI.
# Usage:  .\start-ui.ps1
$root = $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "src"

# Prefer the project venv if it works on this machine, else a system Python.
$python = $null
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
foreach ($candidate in @($venvPy, "py", "python")) {
    try { & $candidate -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $python = $candidate; break } } catch {}
}
if (-not $python) { Write-Error "No working Python found."; exit 1 }

Write-Host "Starting API with: $python"
Start-Process -FilePath $python -ArgumentList "-m", "discovery_agent.api", "--port", "8000" -WorkingDirectory $root
Start-Sleep 2

Push-Location (Join-Path $root "frontend")
if (-not (Test-Path node_modules)) { npm install --no-audit --no-fund }
Start-Process "http://localhost:5173"
npm run dev
Pop-Location
