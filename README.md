# GPU Monitor (SeaTalk Bot)

Multi-project GPU monitor: periodically checks AIS project GPU quota (experiment + notebook),
logs to local JSONL + Google Sheet, and sends conditional SeaTalk alerts.

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
| SeaTalk (verify/group) | Every alert check interval, only if triggered | Markdown alert with utilization breakdown + Google Sheet link |

## Usage

```bash
# One-shot check (console + JSONL + GSheet, no SeaTalk)
python3 aigc_gpu_alert.py

# Continuous monitoring, log only
python3 aigc_gpu_alert.py --loop

# Continuous + SeaTalk verify (private message to verify_email)
python3 aigc_gpu_alert.py --loop --verify

# Continuous + SeaTalk group
python3 aigc_gpu_alert.py --loop --send-group

# Continuous + verify, checking alert condition every 60 seconds
python3 aigc_gpu_alert.py --loop --verify --seatalk-interval 60

# Dry-run (console only, no writes)
python3 aigc_gpu_alert.py --dry-run

# Test SeaTalk connectivity (run on the online service)
python3 test_seatalk.py
```

## Arguments

| Arg | Default | Description |
|-----|---------|-------------|
| `--loop` | off | Continuous monitoring |
| `--verify` | off | Enable SeaTalk private alerts to `seatalk.verify_email` |
| `--send-group` | off | Enable SeaTalk group alerts to `seatalk.group_id` |
| `--interval` | 60 (from config) | Poll interval in seconds |
| `--seatalk-interval` | `alert.check_interval_seconds` (fallback `intervals.seatalk_seconds`, then 3600) | Alert evaluation interval in seconds |
| `--dry-run` | off | Preview only: console output, no JSONL/GSheet/SeaTalk writes |

SeaTalk is never enabled by default. It only runs when `--verify` or
`--send-group` is provided, and it still sends only when the configured alert
condition is true.

## SeaTalk Alert Logic

SeaTalk alert behavior is configured under `alert` in
`config/monitor_config.json`.

- 中文详细逻辑：主循环按 `--interval` / `intervals.poll_seconds` 轮询 AIS，
  默认是每 60 秒探测一次。每一轮都会拉取 GPU quota、更新内存历史、打印
  console，并写入 JSONL / Google Sheet。
- 平均利用率窗口只有一个配置：`status.window_minutes`。console 展示、
  SeaTalk 消息内容、以及 SeaTalk 告警触发判断都用这个窗口；当前配置是
  `180` 分钟。
- SeaTalk 不是每轮都检查。只有启动参数带了 `--verify` 或 `--send-group`，
  且当前时间距离上一次“告警检查”已经达到 `--seatalk-interval` /
  `alert.check_interval_seconds` 时，才进入 SeaTalk 告警判断。
- 计时锚点是 `last_alert_check`，含义是“上一次执行告警检查的时间”，不是
  “上一次成功发出 SeaTalk 消息的时间”。代码在进入告警检查后会立刻把
  `last_alert_check` 更新为当前时间，所以即使这次因为不在发送时间段、利用率
  正常、或发送失败而没有真正发消息，也会重新开始计算下一次检查间隔。
- 服务刚启动时 `last_alert_check = 0`，所以第一轮探测完成后会立刻做一次
  SeaTalk 告警检查；如果当时在允许发送时间段内，并且触发条件成立，就会马上发
  一条。后续才按 `seatalk_interval` 间隔检查。
- 因此它更接近“从上一次告警检查时间开始计时”：例如
  `poll_seconds=60`、`seatalk_interval=3600` 时，10:00 启动会在 10:00
  第一轮检查；如果 10:00 正常、10:30 变低，不会在 10:30 立即发，而是等到
  11:00 左右下一次告警检查时，如果仍满足条件才发。由于只有轮询时才执行逻辑，
  实际时间可能比配置间隔晚最多一个 poll 周期。
- 当前 `start.sh` 使用 `--seatalk-interval 60`，这会让 SeaTalk 告警判断也
  基本每分钟执行一次；在这种启动方式下，只要每分钟检查时仍处于允许发送时间段
  且利用率低，就可能每分钟发一条。
- Schedule gate: alerts are only allowed within `alert.schedule.weekdays`,
  `start_time`, and `end_time`.
- Timezone: `alert.schedule.timezone` is used for console timestamps, JSONL
  timestamps, Google Sheet tab dates, SeaTalk message timestamps, and schedule
  checks. The default is `Asia/Shanghai`.
- Trigger gate: the default trigger sends when any pool has rolling
  `exp_util < 70%` over `status.window_minutes`.
- Healthy case: if all pools are above threshold, the loop prints
  `[SeaTalk] All pools OK, no alert sent.`
- Token behavior: before initialization checks and before each actual send, the
  script requests a fresh SeaTalk `app_access_token`. The local token cache is
  written only as a debugging aid and is not reused for sending.

Current alert message shape:

```markdown
**GPU Monitor**
`2026-07-30 10:50:55`
---
1. 🔴 **Listing B300** | `hybrid-sg16`
   - Current: `0/6` GPUs (0%)
   - Experiment: `0/2` | Notebook: `0/4`
   - **3h00m avg: exp 68%** | nb 50% | total 64%

---
link: https://docs.google.com/spreadsheets/d/<spreadsheet_id>/edit
```

## Utilization Calculation

- Utilization is calculated from in-memory history only (data since last service restart).
- `util% = mean(used/quota)` across snapshots in `status.window_minutes`.
- Broken down by experiment and notebook.
- Memory keeps at least `status.window_minutes`, with a minimum of 6 hours.

## Google Sheet

- Spreadsheet: configured in `config/monitor_config.json`.
- Each day creates a new worksheet tab named `YYYY-MM-DD` in the configured alert timezone.
- 1 row per minute, columns: `timestamp | pool1_exp_used | pool1_exp_total | ... | pool3_total_quota`.
- Sheets older than 7 days are auto-deleted.
- Service account must have Editor access to the spreadsheet.
- SeaTalk alerts include a direct link to this spreadsheet.

## start.sh

`start.sh` is the notebook-friendly launcher. It installs runtime dependencies,
stops the previous `pid.txt` process if it is still this monitor, and starts:

```bash
python3 -u aigc_gpu_alert.py --loop --verify --seatalk-interval 60
```

It also installs `tzdata` so `Asia/Shanghai` can be resolved on minimal Linux
notebook images.

## Docker

```bash
docker build -t gpu-monitor .
docker run -d --name gpu-monitor gpu-monitor
# Override: log only (no SeaTalk)
docker run -d --name gpu-monitor gpu-monitor python3 aigc_gpu_alert.py --loop
```

## Config Files

### config/monitor_config.json

Main runtime config:

- `pools`: AIS projects/zones/GPU models to monitor.
- `seatalk.group_id`: target group for `--send-group`.
- `seatalk.verify_email`: private target for `--verify`; resolved to
  `employee_code` at startup.
- `gsheet.spreadsheet_id`: Google Sheet ID used for writes and the SeaTalk
  message `link:`.
- `intervals.poll_seconds`: default monitor poll interval.
- `status.metric` and `status.window_minutes`: rolling metric and averaging
  window used by console, SeaTalk messages, and SeaTalk alert trigger checks.
- `status.styles`: icon/label display rules. Empty labels are preserved and not
  rendered as Markdown `****`.
- `alert.check_interval_seconds`: default alert evaluation interval.
- `alert.trigger`: metric, threshold, and condition. The rolling window comes
  from `status.window_minutes`.
- `alert.schedule`: timezone, weekdays, and allowed send window.

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
