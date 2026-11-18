# OPTIONAL: registers a Windows Task Scheduler job that runs the no-reply deadline check every hour,
# even when the dashboard is closed. Nothing registers this automatically — run it yourself if you want it.
#   Register:   powershell -ExecutionPolicy Bypass -File scripts\schedule_deadline_check.ps1
#   Remove:     powershell -ExecutionPolicy Bypass -File scripts\schedule_deadline_check.ps1 -Remove
param([switch]$Remove, [int]$EveryMinutes = 60)
$TaskName = "SprintoAgentDeadlineCheck"
$Root = Split-Path -Parent $PSScriptRoot
if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed task $TaskName"
    exit 0
}
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Action = New-ScheduledTaskAction -Execute $Python -Argument "run.py --check-deadlines" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Description "Sprinto demo agent: escalate threads past the no-reply deadline" -Force | Out-Null
Write-Output "Registered $TaskName (every $EveryMinutes min, current user). Remove with -Remove."
