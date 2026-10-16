[CmdletBinding()]
param([string]$TaskName = 'LemonCookieBot')
$ErrorActionPreference = 'Stop'
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Task) {
    Stop-ScheduledTask -TaskName $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host 'Task removed. Code, database, tokens and backups were kept.'
} else {
    Write-Host 'Task does not exist. No changes made.'
}
