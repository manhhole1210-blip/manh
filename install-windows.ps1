# Run in an elevated PowerShell window from the unpacked bot directory.
[CmdletBinding()]
param(
    [ValidatePattern('^\d*$')][string]$GuildId = '',
    [ValidatePattern('^[\d,]*$')][string]$AdminIds = '',
    [ValidatePattern('^\d+$')][string]$AlertChannelId = '0',
    [string]$TaskName = 'LemonCookieBot'
)
$ErrorActionPreference = 'Stop'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell as Administrator, then run this script again.'
}
$BotRoot = $PSScriptRoot
Set-Location -LiteralPath $BotRoot
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Task $TaskName already exists. Stop it before updating; use Start-ScheduledTask afterward."
}
$PythonExe = Join-Path $BotRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv (Join-Path $BotRoot '.venv')
    } else {
        & python -m venv (Join-Path $BotRoot '.venv')
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create venv. Install Python 3.12 first.' }
}
& $PythonExe -m pip install -r (Join-Path $BotRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if ($GuildId -or $AdminIds) {
    if (-not $GuildId -or -not $AdminIds) { throw 'Provide both GuildId and AdminIds.' }
    $ConfigArguments = @('manage_bot.py', 'configure', '--guild-id', $GuildId, '--alert-channel-id', $AlertChannelId)
    foreach ($AdminId in $AdminIds.Split(',')) {
        if (-not $AdminId) { throw 'AdminIds contains an empty ID.' }
        $ConfigArguments += @('--admin-id', $AdminId)
    }
    & $PythonExe @ConfigArguments
    if ($LASTEXITCODE -ne 0) { throw 'Configuration failed. Stop any running bot before retrying.' }
}
$TokenFile = Join-Path $BotRoot 'input\token.txt'
if (-not (Test-Path -LiteralPath $TokenFile) -or (Get-Item -LiteralPath $TokenFile).Length -eq 0) {
    throw 'Task runs as SYSTEM: store the Discord token locally in input\token.txt before installation. Do not paste it in chat.'
}
$Runner = Join-Path $BotRoot 'run_bot.py'
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument ('"{0}"' -f $Runner) -WorkingDirectory $BotRoot
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$TaskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $TaskPrincipal | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host 'Installed. Bot starts at Windows boot, even when Remote Desktop is disconnected.'
Write-Host "Log file: $BotRoot\logs\bot.log"
