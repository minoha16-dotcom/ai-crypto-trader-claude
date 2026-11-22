[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^202[3-6]-(0[1-9]|1[0-2])$')]
    [string]$Month,

    [Parameter(Mandatory=$true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
    [string]$DriveRoot,

    [string]$LocalRoot = 'C:\Vike_Data\Vike_V3.1',
    [string]$Symbols = 'BTCUSDT',
    [double]$DelaySeconds = 0.20
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Collector = Join-Path $RepoRoot 'scripts\binance_usdm_resumable_collector.py'

if (-not (Test-Path -LiteralPath $Collector -PathType Leaf)) {
    throw "Collector not found: $Collector"
}
if ($Month -lt '2023-11' -or $Month -gt '2026-04') {
    throw "Only development months 2023-11 through 2026-04 are allowed. The 2026-05 through 2026-10 holdout is locked."
}
if ($DelaySeconds -lt 0) { throw 'DelaySeconds cannot be negative.' }

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw 'Python was not found. Install Python 3.11 and reopen PowerShell.' }
$pythonVersion = & python --version 2>&1
Write-Host "Python: $pythonVersion"

& python -c "import requests, urllib3" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw 'Python dependencies missing. Run: python -m pip install requests urllib3'
}

$start = [DateTime]::SpecifyKind([DateTime]::ParseExact("$Month-01", 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture), [DateTimeKind]::Utc)
$end = $start.AddMonths(1)
$startIso = $start.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
$endIso = $end.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")

$monthLocal = Join-Path $LocalRoot "raw-staging\$Month"
$monthDrive = Join-Path $DriveRoot "raw-staging\$Month"
New-Item -ItemType Directory -Force -Path $monthLocal, $monthDrive | Out-Null

Write-Host "Vike V3.1 monthly collection"
Write-Host "Month: $Month UTC [$startIso, $endIso)"
Write-Host "Symbols: $Symbols"
Write-Host "Local resumable staging: $monthLocal"
Write-Host "Drive sync destination: $monthDrive"
Write-Host 'The collector resumes from its local checkpoints. Do not delete the local staging folder.'

Push-Location $RepoRoot
try {
    & python $Collector --symbols $Symbols --start $startIso --end $endIso --out-dir $monthLocal --delay $DelaySeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Collector failed with exit code $LASTEXITCODE. Local checkpoint is retained; rerun the same command to resume."
    }
}
finally {
    Pop-Location
}

# Copy only after the collector exits successfully. Local files are retained for
# restart/recovery; this script never deletes raw data.
$robocopyLog = Join-Path $monthLocal 'robocopy-drive.log'
& robocopy $monthLocal $monthDrive /E /COPY:DAT /DCOPY:DAT /R:3 /W:5 /FFT /NP /LOG:$robocopyLog
$robocopyCode = $LASTEXITCODE
if ($robocopyCode -ge 8) {
    throw "Robocopy failed (exit $robocopyCode). Local staging is retained; retry the same month."
}

# Verify each copied file byte-for-byte at the local Drive-for-desktop sync path.
$files = Get-ChildItem -LiteralPath $monthLocal -File | Where-Object { $_.Name -ne 'robocopy-drive.log' }
foreach ($file in $files) {
    $destination = Join-Path $monthDrive $file.Name
    if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
        throw "Drive sync folder copy missing: $destination"
    }
    $sourceHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    $destHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
    if ($sourceHash -ne $destHash) {
        throw "SHA-256 mismatch for $($file.Name). Local staging retained; do not use this month."
    }
}

Write-Host ''
Write-Host 'LOCAL COPY VERIFIED: SHA-256 matches for all copied files.'
Write-Host 'IMPORTANT: confirm Google Drive for desktop reports "Synced" before treating the cloud upload as complete.'
Write-Host 'No audit approval, labels, model training, or raw-data deletion is performed by this script.'
