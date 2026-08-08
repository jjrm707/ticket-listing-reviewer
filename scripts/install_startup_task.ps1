$ErrorActionPreference = "Stop"

$taskName = "TicketListingReviewer"
$taskPath = "\"
$repoRoot = Split-Path -Parent $PSScriptRoot
$runScript = Join-Path $PSScriptRoot "run.ps1"

if (-not (Test-Path -LiteralPath $repoRoot -PathType Container)) {
    throw "Application directory is unavailable."
}
if (-not (Test-Path -LiteralPath $runScript -PathType Leaf)) {
    throw "Application launcher is unavailable."
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$actionArguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $runScript
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $actionArguments `
    -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $taskName `
    -TaskPath $taskPath `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $taskSettings `
    -Force | Out-Null

Write-Host "Scheduled task TicketListingReviewer is installed. Sign out and back in to start it."
