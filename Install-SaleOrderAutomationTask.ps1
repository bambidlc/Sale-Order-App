param(
    [string]$TaskName = 'Sale Order App Shop Orders Automation',
    [string]$PythonExe = 'C:\Python314\python.exe',
    [int]$MaxMessages = 100
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$wrapper = Join-Path $projectRoot 'Start-SaleOrderAutomationScheduled.ps1'
$powerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`" -PythonExe `"$PythonExe`" -MaxMessages $MaxMessages"

$action = New-ScheduledTaskAction -Execute $powerShellExe -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 2)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

$registeredTask = Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Processes Shop Reportes Ordenes into Odoo and permanently deletes each exact source message only after success.' `
    -Force `
    -ErrorAction Stop

if ($null -eq $registeredTask) {
    throw "Scheduled task registration did not return a task object: $TaskName"
}

Write-Host "Installed scheduled task: $TaskName" -ForegroundColor Green
