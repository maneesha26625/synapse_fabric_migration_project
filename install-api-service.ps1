# Keeps the Migration Accelerator API running in the background: a scheduled task for the current user that
# starts it (hidden, with auto-reload) when you sign in to Windows, and now.
#
#   .\install-api-service.ps1             install (or update) and start it
#   .\install-api-service.ps1 -Remove     stop it and remove the task
#
# No administrator rights needed: the task runs as you, only while you are signed in.
param([switch]$Remove)
$name = "MigrationAcceleratorAPI"
$root = $PSScriptRoot

function Stop-Api {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='py.exe'" |
        Where-Object { $_.CommandLine -like '*discovery_agent.api*--port 8001*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Remove) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Stop-Api
    Write-Host "Removed the task '$name' and stopped the API."
    exit 0
}

$script = Join-Path $root "api-service.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $root `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# No time limit, keep running on battery, restart if it ever stops with an error.
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Migration Accelerator API (port 8001, auto-reload). Log: $root\.accelerator\api.log" -Force | Out-Null
Stop-Api   # an API started some other way gives way to the task's
Start-ScheduledTask -TaskName $name
Write-Host "Task '$name' installed and started. The API runs in the background on http://127.0.0.1:8001."
Write-Host "Log: $root\.accelerator\api.log   Remove: .\install-api-service.ps1 -Remove"
