# GPU Monitor (SeaTalk Bot)

Multi-project GPU monitor: periodically checks AIS project GPU quota (experiment + notebook),
logs to local JSONL + Google Sheet, and sends periodic summaries to SeaTalk.

## Monitored Pools

| Pool | Project | Zone | GPU |
|------|---------|------|-----|
| AIGC H100 | 100160 (MPID-Train-AIGC-LLVM) | offline-sg12 | H100-80GiB |
| Listing A100 | 112 (MPID-Listing-Train) | us1 | A100-80GiB |
| Listing B300 | 112 (MPID-Listing-Train) | hybrid-sg16 | B300 |

GPU totals are read dynamically from the API (experiment quota + notebook quota).

## Directory Structure

```
seatalk-bot/
  aigc_gpu_alert.py              # Main monitor script
  test_seatalk.py                # SeaTalk connectivity test (run on online service)
  Dockerfile                     # Container build
  requirements.txt               # Python dependencies
  .dockerignore
  .gitignore
  config/
    monitor_config.json          # Pool definitions, intervals, GSheet/SeaTalk config
    *.example.json               # Safe config templates committed to Git
    ais_config.json              # Local AIS auth (ignored by Git)
    seatalk_credentials.json     # Local SeaTalk Bot credentials (ignored by Git)
    google_service_account.json  # Local Google Service Account (ignored by Git)
  data/                          # JSONL logs (gitignored, auto-created)
  README.md
```

## Configuration

Real credential files under `config/` are local-only and ignored by Git. Start
from the tracked templates:

```bash
cp config/ais_config.example.json config/ais_config.json
cp config/seatalk_credentials.example.json config/seatalk_credentials.json
cp config/google_service_account.example.json config/google_service_account.json
```

Fill in the copied files on the machine or service that runs the monitor. Do
not commit those copied credential files.

## Output Destinations

| Destination | Frequency | Content |
|-------------|-----------|---------|
| Console (stdout) | Every 1 min | 3-line status (used/total per pool) |
| JSONL (`data/gpu_monitor_YYYY-MM-DD.jsonl`) | Every 1 min | Full snapshot with exp/nb breakdown |
| Google Sheet (daily tab) | Every 1 min | 1 row: timestamp + 6 fields per pool |
| SeaTalk (verify/group) | Every 6 hours | Status + utilization breakdown |

## Usage

```bash
# One-shot check (console + JSONL + GSheet, no SeaTalk)
python3 aigc_gpu_alert.py

# Continuous monitoring, log only
python3 aigc_gpu_alert.py --loop

# Continuous + SeaTalk verify (private message to yourself)
python3 aigc_gpu_alert.py --loop --verify

# Continuous + SeaTalk group
python3 aigc_gpu_alert.py --loop --send-group

# Dry-run (console only, no writes)
python3 aigc_gpu_alert.py --dry-run

# Test SeaTalk connectivity (run on the online service)
python3 test_seatalk.py
```

## Arguments

| Arg | Default | Description |
|-----|---------|-------------|
| `--loop` | off | Continuous monitoring |
| `--verify` | off | SeaTalk to yourself only |
| `--send-group` | off | SeaTalk to group chat |
| `--interval` | 60 (from config) | Poll interval in seconds |
| `--seatalk-interval` | 21600 (from config) | SeaTalk send interval in seconds |
| `--dry-run` | off | Preview only |

## Utilization Calculation

- Utilization is calculated from in-memory history only (data since last service restart).
- `util% = mean(used/quota)` across all 1-min snapshots in the window.
- Broken down by experiment and notebook.
- Memory is cleaned every 6 hours to prevent OOM.

## Google Sheet

- Spreadsheet: configured in `config/monitor_config.json`.
- Each day creates a new worksheet tab named `YYYY-MM-DD`.
- 1 row per minute, columns: `timestamp | pool1_exp_used | pool1_exp_total | ... | pool3_total_quota`.
- Sheets older than 7 days are auto-deleted.
- Service account must have Editor access to the spreadsheet.

## Docker

```bash
docker build -t gpu-monitor .
docker run -d --name gpu-monitor gpu-monitor
# Override: log only (no SeaTalk)
docker run -d --name gpu-monitor gpu-monitor python3 aigc_gpu_alert.py --loop
```

## Config Files

### config/monitor_config.json

Pool definitions, intervals, GSheet spreadsheet ID, SeaTalk group/verify config.

### config/ais_config.json

Local-only AIS credentials. Copy from `config/ais_config.example.json` and fill
in the token:

```json
{"auth": {"email": "...", "token": "<AIS_PAT>", "host": "https://ais.mlp.shopee.io"}}
```

### config/seatalk_credentials.json

Local-only SeaTalk Open Platform credentials. Copy from
`config/seatalk_credentials.example.json` and fill in the app values:

```json
{"seatalk_open_platform": {"host": "https://openapi.seatalk.io", "app_id": "...", "app_secret": "...", "self_email": "..."}}
```

### config/google_service_account.json

Local-only Google Service Account credentials for GSheet writes. Copy from
`config/google_service_account.example.json`, then replace the placeholders with
the downloaded key.
Download from [Google Cloud Console](https://console.cloud.google.com/).
