$ErrorActionPreference = "Stop"

$taskName = "TicketListingReviewer"
$taskPath = "\"
$existingTask = Get-ScheduledTask `
    -TaskName $taskName `
    -TaskPath $taskPath `
    -ErrorAction SilentlyContinue

if ($null -eq $existingTask) {
    Write-Host "Scheduled task TicketListingReviewer is not installed."
    return
}

Write-Host "Removing scheduled task TicketListingReviewer."
Unregister-ScheduledTask `
    -TaskName $taskName `
    -TaskPath $taskPath `
    -Confirm:$false
Write-Host "Scheduled task TicketListingReviewer was removed."
