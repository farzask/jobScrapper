# Registers the daily sweep with Windows Task Scheduler.
#
#   Run once, from an ordinary (non-admin) PowerShell:
#       powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#
#   Remove it later with:
#       Unregister-ScheduledTask -TaskName "JobApplier Daily Sweep" -Confirm:$false

$ErrorActionPreference = "Stop"

$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$script = Join-Path $root "scripts\daily_sweep.py"
$name   = "JobApplier Daily Sweep"

if (-not (Test-Path $python)) { throw "venv not found at $python - create it first" }
if (-not (Test-Path $script)) { throw "daily_sweep.py not found at $script" }

$action  = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At 8:00am

# StartWhenAvailable matters on a laptop: if the machine was asleep at 8am the
# sweep still runs once it wakes, instead of silently skipping the day.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Settings $settings -Description "Finds new jobs, enriches them, exports CSV." `
    -Force | Out-Null

Write-Output "Registered '$name' - runs daily at 8:00 AM."
Write-Output "Test it now with:  Start-ScheduledTask -TaskName `"$name`""
