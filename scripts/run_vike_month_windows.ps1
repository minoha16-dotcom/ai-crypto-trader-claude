[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^202[3-6]-(0[1-9]|1[0-2])$')]
    [string]$Month,
    [Parameter(Mandatory=$true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
    [string]$DriveRoot,
    [string]$Symbols = 'BTCUSDT',
    [double]$DelaySeconds = 0.20
)
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Collector = Join-Path $RepoRoot 'scripts\binance_usdm_resumable_collector.py'
$Integrity = Join-Path $RepoRoot 'scripts\vike_month_integrity.py'
foreach ($required in @($Collector, $Integrity)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Required script not found: $required" }
}
if ($Month -lt '2023-11' -or $Month -gt '2026-04') {
    throw "Only development months 2023-11 through 2026-04 are allowed. The 2026-05 through 2026-10 holdout is locked."
}
if ($DelaySeconds -lt 0) { throw 'DelaySeconds cannot be negative.' }
$requestedSymbols = @($Symbols.Split(',') | ForEach-Object { $_.Trim().ToUpperInvariant() } | Where-Object { $_ })
if ($requestedSymbols.Count -eq 0 -or ($requestedSymbols | Select-Object -Unique).Count -ne $requestedSymbols.Count) { throw 'Symbols must be non-empty and unique.' }
foreach ($symbol in $requestedSymbols) { if ($symbol -notmatch '^[A-Z0-9]+$') { throw "Invalid symbol: $symbol" } }
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw 'Python was not found. Install Python 3.11 and reopen PowerShell.' }
Write-Host ("Python: " + (& python --version 2>&1))
& python -c "import requests, urllib3" 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies missing. Run: python -m pip install requests urllib3' }
$start = [DateTime]::SpecifyKind([DateTime]::ParseExact("$Month-01", 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture), [DateTimeKind]::Utc)
$end = $start.AddMonths(1)
$startIso = $start.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
$endIso = $end.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
$monthLocal = Join-Path $DriveRoot "raw-staging\$Month"
$logRoot = Join-Path $DriveRoot 'logs'
New-Item -ItemType Directory -Force -Path $monthLocal, $logRoot | Out-Null
Write-Host 'Vike V3.1 monthly collection + integrity pipeline'
Write-Host "Month: $Month UTC [$startIso, $endIso)"
Write-Host "Symbols: $($requestedSymbols -join ',')"
Write-Host "Primary collection and storage (Google Drive sync folder): $monthLocal"
Write-Host "No separate local raw-data staging or Robocopy step is used."
Push-Location $RepoRoot
try {
    & python $Collector --symbols ($requestedSymbols -join ',') --start $startIso --end $endIso --out-dir $monthLocal --delay $DelaySeconds
    if ($LASTEXITCODE -ne 0) { throw "Collector failed ($LASTEXITCODE). Drive-folder checkpoint retained; rerun the same month to resume after confirming Google Drive sync status." }
    foreach ($symbol in $requestedSymbols) {
        $checkpointPath = Join-Path $monthLocal "$symbol-checkpoint.json"
        if (-not (Test-Path -LiteralPath $checkpointPath -PathType Leaf)) { throw "Missing checkpoint for $symbol; refusing Drive copy." }
        $checkpoint = Get-Content -LiteralPath $checkpointPath -Raw | ConvertFrom-Json
        if ($checkpoint.symbol -ne $symbol -or $checkpoint.start_ms -ne ([DateTimeOffset]$start).ToUnixTimeMilliseconds() -or $checkpoint.end_ms_exclusive -ne ([DateTimeOffset]$end).ToUnixTimeMilliseconds()) { throw "Checkpoint scope mismatch for $symbol." }
        foreach ($stream in @('trade_15m','mark_15m','funding')) {
            $streamState = $checkpoint.streams.PSObject.Properties[$stream]
            if (-not $streamState -or $streamState.Value.complete -ne $true) { throw "Incomplete $symbol/$stream checkpoint; refusing Drive copy." }
        }
    }
    $reportPath = Join-Path $monthLocal 'month-audit.json'
    $manifestPath = Join-Path $monthLocal 'manifest.json'
    & python $Integrity audit --month $Month --month-dir $monthLocal --symbols ($requestedSymbols -join ',') --report $reportPath --manifest $manifestPath
    if ($LASTEXITCODE -ne 0) { throw "Monthly integrity audit failed ($LASTEXITCODE); Drive raw-staging retained for diagnosis and resume." }
}
finally { Pop-Location }
Write-Host ''
Write-Host 'PASS: data integrity, daily audit, and manifest generated in the Google Drive sync folder.'
Write-Host 'CLOUD SYNC: NOT VERIFIED by this script. Confirm Google Drive for desktop reports Synced before treating the month as backed up.'
Write-Host 'Funding coverage remains NOT_VERIFIED. No labels, training, model approval, holdout access, or raw-data deletion.'
