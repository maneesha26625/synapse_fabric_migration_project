# Runs the Migration Accelerator API in the background, with auto-reload. Used by the Windows scheduled task
# "MigrationAcceleratorAPI" (see install-api-service.ps1); can also be run by hand.
#
# Log: .accelerator\api.log. The API restarts itself when backend code changes, and refuses to start a second
# copy if one already answers on port 8001, so running this twice is harmless.
$root = $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONUNBUFFERED = "1"
$logDir = Join-Path $root ".accelerator"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "api.log"

$python = $null
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
foreach ($candidate in @($venvPy, "py", "python")) {
    try { & $candidate -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $python = $candidate; break } } catch {}
}
if (-not $python) { Add-Content $log "$(Get-Date -Format s)  No working Python found."; exit 1 }

Set-Location $root
Add-Content $log "$(Get-Date -Format s)  Starting the API with $python" -Encoding utf8
# Through cmd so the log stays plain text (PowerShell 5.1 would write it as UTF-16).
cmd /c "`"$python`" -m discovery_agent.api --port 8001 --reload >> `"$log`" 2>&1"
