# Windows local collection and Google Drive desktop sync

## What this setup does

Run the existing resumable Binance USD-M collector on the user's Windows PC.
It writes to a durable local staging directory first, then copies the completed
month into a Google Drive for desktop synced folder. It verifies SHA-256 between
the local source and the local Drive sync path. This local hash check does not
prove cloud sync completion: wait for Google Drive for desktop to show Synced.

No GitHub upload, OAuth token, service-account key, or Google password is needed.
The user's Google Drive account must have Editor permission on the destination
folder. For a shared folder, add a shortcut to My Drive if needed so Drive for
desktop exposes it locally.

## One-time setup

1. Install Google Drive for desktop:
   https://www.google.com/drive/download/
2. Sign in to the Google account that has Editor access to the shared folder.
3. In Drive on the web, add the shared Vike folder to My Drive (Add shortcut to
   Drive) if it is not already visible in Drive for desktop.
4. Wait until Drive for desktop is running. Find the local folder corresponding
   to the shared folder, open it in File Explorer, and copy its actual local
   path. Do not assume it is always G:; the drive letter is configurable.
5. Clone or update this repository and check out the Vike V3.1 branch.
6. In PowerShell, from the repository root:
   ```powershell
   python --version
   python -m pip install requests urllib3
   ```
   Python 3.11 is recommended.

## First safe pilot: BTCUSDT, one month

From the repository root, use the actual local path to the synced shared folder:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\run_vike_month_windows.ps1 -Month 2023-11 -DriveRoot "G:\My Drive\Vike V3.1" -Symbols BTCUSDT
```

Replace the example DriveRoot with the path found on the user's PC. The script
will:
- enforce the development window 2023-11 through 2026-04;
- collect Trade 15m, Mark Price 15m, and Funding using the resumable collector;
- retain local checkpoints under C:\Vike_Data\Vike_V3.1\raw-staging\YYYY-MM;
- copy the completed month into the synced Drive folder;
- compare SHA-256 for each local copied file;
- never delete local raw files.

If interrupted, rerun the exact same month/symbol command. Do not change the
month window or delete the local staging directory. The collector checkpoint
scope must match.

## Eight-asset monthly collection

Only after the BTCUSDT pilot has been independently audited and approved:

```powershell
.\scripts\run_vike_month_windows.ps1 -Month 2023-11 -DriveRoot "G:\My Drive\Vike V3.1" -Symbols BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT
```

Repeat month by month. Do not launch all 36 months until the monthly pilot,
storage capacity, API stability, and independent coverage audits pass.

## Critical limitations

- This script performs collection and local Drive-folder mirroring only.
- The existing collector does not prove historical Funding coverage; empty API
  pages mean pagination exhaustion, not coverage PASS.
- Mark Price, Funding archive semantics, historical contract availability,
  and the November 2023 Mark Price gap require independent audits.
- No missing bars/events are filled.
- This script does not create ATR/features/labels, run model training, or approve
  a model.
- The 2026-05 through 2026-10 locked holdout is rejected by this script and
  must not be used for training or tuning.
- Do not delete local raw staging until cloud sync is complete and a separate
  audited durable copy exists.
