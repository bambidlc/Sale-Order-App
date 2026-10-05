param(
    [string]$PythonExe = 'C:\Python314\python.exe',
    [int]$MaxMessages = 100
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $projectRoot 'run_orders_automation.py'
$logDir = Join-Path $projectRoot 'Scheduler_Logs'
$logPath = Join-Path $logDir 'sale_order_automation_scheduler.log'
$mutex = New-Object System.Threading.Mutex($false, 'Local\SaleOrderAppShopOrdersAutomation')
$hasLock = $false

try {
    try { $hasLock = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $hasLock = $true }
    if (-not $hasLock) {
        exit 0
    }

    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
        $trimmed = Get-Content -LiteralPath $logPath -Tail 5000
        Set-Content -LiteralPath $logPath -Value $trimmed -Encoding utf8
    }

    Add-Content -LiteralPath $logPath -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Starting Shop order automation."
    Push-Location $projectRoot
    try {
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $PythonExe -u $runner --max-messages $MaxMessages 2>&1 | ForEach-Object {
                $cleanLine = ($_.ToString() -replace [string][char]0, '')
                Add-Content -LiteralPath $logPath -Value $cleanLine -Encoding utf8
            }
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
    }
    finally {
        Pop-Location
    }

    if ($exitCode -eq 0) {
        Add-Content -LiteralPath $logPath -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Shop order automation completed successfully."
    }
    else {
        Add-Content -LiteralPath $logPath -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Shop order automation failed. exit_code=$exitCode"
    }
    exit $exitCode
}
catch {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    Add-Content -LiteralPath $logPath -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Scheduler wrapper failed: $($_.Exception.Message)"
    exit 1
}
finally {
    if ($hasLock) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
