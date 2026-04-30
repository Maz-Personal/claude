# Registers a Windows Task Scheduler job to run the Capitol Copier bot every 30 minutes.
# Run this script once as Administrator.

$taskName    = "CapitolCopierBot"
$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe   = (Get-Command python).Source
$botScript   = Join-Path $scriptDir "bot.py"

$action  = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$botScript`"" -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 30) -Once -At (Get-Date)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force

Write-Host "Task '$taskName' registered — runs every 30 minutes."
Write-Host "To run immediately: Start-ScheduledTask -TaskName '$taskName'"
Write-Host "To remove:          Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"
