<#
Register (or refresh) the Windows Task Scheduler entry that runs the
background harvester at log-on and every few hours after.

    powershell -ExecutionPolicy Bypass -File tools\register_harvest_task.ps1
    powershell -ExecutionPolicy Bypass -File tools\register_harvest_task.ps1 -Every 8
    powershell -ExecutionPolicy Bypass -File tools\register_harvest_task.ps1 -Remove

Runs JobHarvester.exe from the repo root when it exists (build it with
`python build_app.py --target harvest`), else `python harvest.py` from the
`jobs` conda environment. Each run pulls the boards not harvested in the
last 6 hours and exits, so the repeat trigger is the loop: no process sits
waiting between runs. Task Scheduler skips a firing while the previous run
is still going, and the harvester's own lock file catches a manual start
that overlaps a scheduled one.

The task runs as the current user with the S4U logon type ("run whether
user is logged on or not", no password stored): that is what keeps a
console window from popping up at log-on. Output goes to
data\logs\session-harvest-*.log either way.
#>
param(
    [int]$Every = 12,          # hours between runs
    [string]$TaskName = "Jobs Harvester",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed task '$TaskName'"
    exit 0
}

$exe = Join-Path $root "JobHarvester.exe"
if (Test-Path $exe) {
    $action = New-ScheduledTaskAction -Execute $exe -WorkingDirectory $root
} else {
    $py = Join-Path $env:USERPROFILE "miniconda3\envs\jobs\python.exe"
    if (-not (Test-Path $py)) { $py = "python" }
    $action = New-ScheduledTaskAction -Execute $py -Argument "harvest.py" `
        -WorkingDirectory $root
}

# At log-on, then every $Every hours for as long as the session lasts.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours $Every)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours ([Math]::Max($Every - 1, 1))) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -Priority 7                       # below-normal: a background job

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "registered '$TaskName': at log-on, then every $Every h, running"
Write-Host "  $($action.Execute) $($action.Arguments) (in $root)"
Write-Host "Start it now with: Start-ScheduledTask -TaskName '$TaskName'"
