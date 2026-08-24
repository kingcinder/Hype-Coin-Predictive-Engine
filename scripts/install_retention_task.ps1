# Serpent Circle — retention autopilot on Windows Task Scheduler.
#
# Creates a daily scheduled task that runs one retention pass (Parquet
# compaction + pruning + lake-growth report into Feed Health). Run from an
# elevated PowerShell prompt:
#
#   powershell -ExecutionPolicy Bypass -File scripts/install_retention_task.ps1
#
# Adjust $ProjectRoot / $VenvPython below to your checkout. The task runs as
# the current user at logon-free (highest-available) priority so the lake is
# compacted even when nobody is logged in.

param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$VenvPython = (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    [string]$TaskName = "SerpentRetention"
)

$action = New-ScheduledTaskAction `
    -Execute $VenvPython `
    -Argument "-m ops.retention --once" `
    -WorkingDirectory $ProjectRoot

$trigger = New-ScheduledTaskTrigger -Daily -At 3:00am

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Serpent Circle retention autopilot: compact raw evidence to Parquet, prune expired rows, report lake growth in Feed Health." `
    -Force

Write-Host "Installed scheduled task '$TaskName': daily at 3:00am, $VenvPython -m ops.retention --once"
Write-Host "Manual run:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Next run:    (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime"
