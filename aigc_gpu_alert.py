#!/usr/bin/env python3
"""
Multi-Project GPU Monitor (Conditional Alert)

Periodically checks GPU utilization for multiple AIS projects/zones,
logs to local JSONL + Google Sheet, and sends a conditional alert to
SeaTalk only when at least one pool has danger-level utilization.

Alert logic (configurable in config/monitor_config.json -> "alert"):
  - Every check_interval (default 1H), evaluate each pool's exp GPU
    utilization over a rolling window (default 3H).
  - If ANY pool's average is below the threshold (default 70%), send
    the full status message.  Otherwise stay silent.

Usage:
    python3 aigc_gpu_alert.py                        # one-shot, print + log
    python3 aigc_gpu_alert.py --loop                 # continuous (1-min poll)
    python3 aigc_gpu_alert.py --loop --verify        # continuous, conditional alert to self
    python3 aigc_gpu_alert.py --loop --send-group    # continuous, conditional alert to group
    python3 aigc_gpu_alert.py --dry-run              # preview only, no writes

Config files loaded from ./config/ (relative to this script).
"""

import argparse
import json
import os
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ============================= PATHS ==============================
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"
DATA_DIR = SCRIPT_DIR / "data"

# ========================== SSL CONTEXT ===========================
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _enable_live_logs():
    """Force line-buffered stdout/stderr so nohup logs update immediately."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream:
            continue
        try:
            stream.reconfigure(line_buffering=True, write_through=True)
        except (AttributeError, ValueError):
            pass


# ========================= HTTP HELPERS ===========================

def _http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def _http_post(url, payload, headers=None, timeout=20):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


# ========================= CONFIG LOADING =========================

def load_monitor_config():
    path = CONFIG_DIR / "monitor_config.json"
    if not path.exists():
        sys.exit(f"[ERROR] Monitor config not found: {path}")
    with open(path) as f:
        cfg = json.load(f)

    status_cfg = cfg.setdefault("status", {})
    status_cfg.setdefault("metric", "exp_util")
    status_cfg.setdefault("window_minutes", 8)

    thresholds = status_cfg.setdefault("thresholds", {})
    thresholds.setdefault("excellent_gt", 90)
    thresholds.setdefault("medium_gte", 70)

    styles = status_cfg.setdefault("styles", {})
    for level, defaults in {
        "excellent": {"icon": "🟢", "label": "优秀"},
        "medium": {"icon": "🟡", "label": "中等"},
        "danger": {"icon": "🔴", "label": "危险", "bold": True},
    }.items():
        style = styles.setdefault(level, {})
        for key, value in defaults.items():
            if key not in style or style[key] is None:
                style[key] = value

    # Defaults for conditional alert config
    alert_cfg = cfg.setdefault("alert", {})
    alert_cfg.setdefault("mode", "conditional")
    alert_cfg.setdefault("check_interval_seconds", 3600)
    trigger = alert_cfg.setdefault("trigger", {})
    trigger.setdefault("metric", "exp_util")
    trigger.setdefault("window_hours", 3)
    trigger.setdefault("threshold_pct", 70)
    trigger.setdefault("condition", "any_below")

    # Defaults for alert schedule (when SeaTalk messages are allowed)
    schedule = alert_cfg.setdefault("schedule", {})
    schedule.setdefault("timezone", "Asia/Shanghai")
    schedule.setdefault("weekdays", [1, 2, 3, 4, 5])
    schedule.setdefault("start_time", "09:30")
    schedule.setdefault("end_time", "19:00")
    return cfg


def load_ais_auth():
    candidates = [
        CONFIG_DIR / "ais_config.json",
        Path.home() / ".ais" / "config.json",
    ]
    for p in candidates:
        if p.exists():
            with open(p) as f:
                cfg = json.load(f)
            auth = cfg.get("auth", {})
            token = auth.get("token")
            host = auth.get("host", "https://ais.mlp.shopee.io")
            if token:
                return token, host.rstrip("/")
    token = os.environ.get("AIS_TOKEN")
    host = os.environ.get("AIS_HOST", "https://ais.mlp.shopee.io").rstrip("/")
    if not token:
        sys.exit("[ERROR] No AIS token. Place config/ais_config.json or set AIS_TOKEN.")
    return token, host


def load_seatalk_credentials():
    candidates = [
        CONFIG_DIR / "seatalk_credentials.json",
        Path.home() / ".config" / "sra" / "credentials.json",
    ]
    for p in candidates:
        if p.exists():
            with open(p) as f:
                creds = json.load(f)
            st = creds.get("seatalk_open_platform", {})
            if st.get("app_id") and st.get("app_secret"):
                return st
    sys.exit("[ERROR] SeaTalk credentials not found.")


# ====================== AIS QUOTA FETCHING ========================

def fetch_pool_snapshot(host, token, pool_cfg):
    """Fetch experiment + notebook GPU usage for one pool.

    Returns dict: {
        "name": str, "project_id": int, "zone": str, "model": str,
        "exp_used": float, "exp_total": float,
        "nb_used": float, "nb_total": float,
        "total_used": float, "total_quota": float,
    }
    """
    project_id = pool_cfg["project_id"]
    target_zone = pool_cfg["zone"]
    target_model = pool_cfg["gpu_model"]

    data = _http_get(
        f"{host}/api/quota/v2/projects/{project_id}/quota",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )

    result = {
        "name": pool_cfg["name"],
        "project_id": project_id,
        "zone": target_zone,
        "model": target_model,
        "exp_used": 0.0, "exp_total": 0.0,
        "nb_used": 0.0, "nb_total": 0.0,
    }

    for item in data.get("data", []):
        qname = item.get("quotaName")
        if qname not in ("experiment", "notebook"):
            continue
        for qi in (item.get("quotaItems") or []):
            if qi.get("zone") != target_zone:
                continue
            for g in (qi.get("gpu") or []):
                model_short = (g.get("productModel") or {}).get("shortName", "")
                if target_model not in model_short:
                    continue
                used = g.get("request", 0)
                total = g.get("quota", 0)
                if qname == "experiment":
                    result["exp_used"] = used
                    result["exp_total"] = total
                elif qname == "notebook":
                    result["nb_used"] = used
                    result["nb_total"] = total

    result["total_used"] = result["exp_used"] + result["nb_used"]
    result["total_quota"] = result["exp_total"] + result["nb_total"]
    return result


def fetch_all_snapshots(host, token, pools):
    snapshots = []
    for pool_cfg in pools:
        try:
            snap = fetch_pool_snapshot(host, token, pool_cfg)
            snapshots.append(snap)
        except Exception as e:
            print(f"[WARN] Failed to fetch {pool_cfg['name']}: {e}", file=sys.stderr)
            snapshots.append({
                "name": pool_cfg["name"],
                "project_id": pool_cfg["project_id"],
                "zone": pool_cfg["zone"],
                "model": pool_cfg["gpu_model"],
                "exp_used": 0, "exp_total": 0,
                "nb_used": 0, "nb_total": 0,
                "total_used": 0, "total_quota": 0,
                "error": str(e),
            })
    return snapshots


# ==================== IN-MEMORY HISTORY ===========================

class MemoryHistory:
    """Ring buffer of snapshots in memory for utilization calculation.

    Keeps up to `max_window_seconds` (default 6h) of data.
    Data older than the window is discarded on each cleanup pass.
    On restart, history starts fresh (no log replay).
    """

    def __init__(self, max_window_seconds=21600):
        self.max_window = max_window_seconds  # 6 hours default
        self.records = []  # list of (timestamp_epoch, snapshots_list)

    def add(self, snapshots):
        now = time.time()
        self.records.append((now, snapshots))
        # Trim records older than the window on every add
        cutoff = now - self.max_window
        self.records = [(t, s) for t, s in self.records if t >= cutoff]

    def get_utilization(self, pool_name, window_seconds=None):
        """Return (exp_util%, nb_util%, total_util%, window_minutes) for a pool.

        Uses data from memory only (since last restart).
        """
        if window_seconds is None:
            window_seconds = self.max_window

        now = time.time()
        cutoff = now - window_seconds

        exp_ratios = []
        nb_ratios = []
        total_ratios = []

        for ts, snapshots in self.records:
            if ts < cutoff:
                continue
            for snap in snapshots:
                if snap["name"] != pool_name:
                    continue
                exp_total = snap.get("exp_total", 0)
                nb_total = snap.get("nb_total", 0)
                total_quota = snap.get("total_quota", 0)
                if exp_total > 0:
                    exp_ratios.append(snap.get("exp_used", 0) / exp_total)
                if nb_total > 0:
                    nb_ratios.append(snap.get("nb_used", 0) / nb_total)
                if total_quota > 0:
                    total_ratios.append(snap.get("total_used", 0) / total_quota)

        if not total_ratios:
            return None

        actual_minutes = len(total_ratios)

        def avg_pct(ratios):
            return round(sum(ratios) / len(ratios) * 100, 1) if ratios else 0.0

        return {
            "exp_util": avg_pct(exp_ratios),
            "nb_util": avg_pct(nb_ratios),
            "total_util": avg_pct(total_ratios),
            "window_minutes": actual_minutes,
        }


# ====================== JSONL LOGGING =============================

def write_jsonl(snapshots, ts_str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date_str = ts_str[:10]  # YYYY-MM-DD
    path = DATA_DIR / f"gpu_monitor_{date_str}.jsonl"
    record = {"ts": ts_str, "pools": snapshots}
    with open(path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ==================== GOOGLE SHEET LOGGING ========================

def _get_gsheet_client():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("[WARN] gspread/google-auth not installed, skipping GSheet.", file=sys.stderr)
        return None, None

    sa_path = CONFIG_DIR / "google_service_account.json"
    if not sa_path.exists():
        print(f"[WARN] Service account not found: {sa_path}", file=sys.stderr)
        return None, None

    creds = Credentials.from_service_account_file(
        str(sa_path),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc, gspread


def _build_gsheet_header(pools):
    """Build header row: timestamp + per-pool columns."""
    header = ["timestamp"]
    for pool in pools:
        prefix = pool["name"].replace(" ", "_")
        for field in ["exp_used", "exp_total", "nb_used", "nb_total",
                      "total_used", "total_quota"]:
            header.append(f"{prefix}_{field}")
    return header


def write_gsheet(spreadsheet_id, snapshots, ts_str, pools_cfg, retention_days=7, now_dt=None):
    gc, gspread_mod = _get_gsheet_client()
    if gc is None:
        return

    try:
        sh = gc.open_by_key(spreadsheet_id)
    except Exception as e:
        print(f"[WARN] Cannot open GSheet: {e}", file=sys.stderr)
        return

    date_str = ts_str[:10]  # YYYY-MM-DD

    # Find or create today's worksheet
    ws = None
    for existing in sh.worksheets():
        if existing.title == date_str:
            ws = existing
            break

    if ws is None:
        header = _build_gsheet_header(pools_cfg)
        ncols = len(header)
        try:
            ws = sh.add_worksheet(title=date_str, rows=1500, cols=ncols)
            col_letter = chr(ord("A") + ncols - 1) if ncols <= 26 else "Z"
            ws.update(f"A1:{col_letter}1", [header])
        except Exception as e:
            print(f"[WARN] Cannot create worksheet {date_str}: {e}", file=sys.stderr)
            return

    # Build single row: timestamp + 6 fields per pool
    row = [ts_str]
    for snap in snapshots:
        row.extend([
            round(snap["exp_used"], 2),
            snap["exp_total"],
            round(snap["nb_used"], 2),
            snap["nb_total"],
            round(snap["total_used"], 2),
            snap["total_quota"],
        ])

    try:
        ws.append_row(row, value_input_option="RAW")
    except Exception as e:
        print(f"[WARN] GSheet append failed: {e}", file=sys.stderr)

    # Retention: delete sheets older than N days
    try:
        cutoff_base = now_dt or datetime.now()
        cutoff_date = (cutoff_base - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        for existing in sh.worksheets():
            title = existing.title
            if len(title) == 10 and title < cutoff_date:
                try:
                    sh.del_worksheet(existing)
                    print(f"[GSheet] Deleted old sheet: {title}")
                except Exception:
                    pass
    except Exception as e:
        print(f"[WARN] GSheet retention cleanup failed: {e}", file=sys.stderr)


# ====================== SEATALK MESSAGING =========================

def _seatalk_token_expires_at(expire_value):
    """SeaTalk may return expire as either epoch seconds or TTL seconds."""
    now = time.time()
    try:
        expire_value = float(expire_value)
    except (TypeError, ValueError):
        expire_value = 7200
    if expire_value > 1_000_000_000:
        return expire_value
    return now + expire_value


def seatalk_get_token(app_id, app_secret, host="https://openapi.seatalk.io"):
    """Get a fresh SeaTalk access token and save it for debugging."""
    cache_path = CONFIG_DIR / ".seatalk_token_cache.json"
    resp = _http_post(
        f"{host}/auth/app_access_token",
        {"app_id": app_id, "app_secret": app_secret},
    )
    if resp.get("code") != 0:
        print(f"[ERROR] SeaTalk token failed: {resp}", file=sys.stderr)
        return None
    resp["host"] = host
    resp["app_id"] = app_id
    resp["expires_at"] = _seatalk_token_expires_at(resp.get("expire", 7200))
    try:
        cache_path.write_text(json.dumps(resp))
    except Exception:
        pass
    return resp["app_access_token"]


def seatalk_resolve_email(email, st_token, host="https://openapi.seatalk.io"):
    resp = _http_post(
        f"{host}/contacts/v2/get_employee_code_with_email",
        {"emails": [email]},
        headers={"Authorization": f"Bearer {st_token}"},
    )
    if resp.get("code") != 0:
        return None
    for emp in resp.get("employees", []):
        if emp.get("email") == email:
            return emp.get("employee_code")
    return None


def seatalk_send_group(group_id, content, st_token, host="https://openapi.seatalk.io"):
    resp = _http_post(
        f"{host}/messaging/v2/group_chat",
        {
            "group_id": group_id,
            "message": {"tag": "text", "text": {"content": content, "format": 1}},
        },
        headers={"Authorization": f"Bearer {st_token}"},
    )
    return resp.get("code") == 0


def seatalk_send_user(employee_code, content, st_token, host="https://openapi.seatalk.io"):
    resp = _http_post(
        f"{host}/messaging/v2/single_chat",
        {
            "employee_code": employee_code,
            "message": {"tag": "text", "text": {"content": content, "format": 1}},
        },
        headers={"Authorization": f"Bearer {st_token}"},
    )
    return resp.get("code") == 0


# ====================== MESSAGE BUILDER ===========================

_FIXED_TZ_OFFSETS = {
    "Asia/Shanghai": 8,
    "Asia/Singapore": 8,
    "Singapore": 8,
    "UTC": 0,
}


def _get_alert_timezone(alert_cfg):
    schedule = (alert_cfg or {}).get("schedule") or {}
    tz_name = schedule.get("timezone", "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:
        # AIS notebooks may lack system tzdata. Keep SG/CN alerts on UTC+8.
        if tz_name in _FIXED_TZ_OFFSETS:
            return timezone(timedelta(hours=_FIXED_TZ_OFFSETS[tz_name]))
        raise


def _now_in_alert_timezone(alert_cfg):
    return datetime.now(_get_alert_timezone(alert_cfg))

def _fmt_gpu(v):
    """Format GPU count: integer if whole, else 1 decimal."""
    return str(int(v)) if v == int(v) else f"{v:.1f}"


def _pct(used, total):
    return (used / total * 100) if total > 0 else 0.0


def _status_style(metric_value, status_cfg):
    """Resolve status style from config thresholds."""
    thresholds = status_cfg.get("thresholds", {})
    styles = status_cfg.get("styles", {})

    if metric_value > thresholds.get("excellent_gt", 90):
        return styles.get("excellent", {})
    if metric_value >= thresholds.get("medium_gte", 70):
        return styles.get("medium", {})
    return styles.get("danger", {})


def _format_status_label(style, markdown=False):
    label = style.get("label", "")
    if not label:
        return ""
    if markdown and style.get("bold"):
        return f"**{label}**"
    return label


def _pool_heading(index, status_icon, name, zone, status_label):
    prefix = f"{index}. "
    parts = [f"{status_icon} **{name}**"]
    if zone:
        parts.append(f"`{zone}`")
    if status_label:
        parts.append(status_label)
    return prefix + " | ".join(parts)


def _metric_display_name(metric):
    return {
        "exp_util": "exp",
        "nb_util": "nb",
        "total_util": "total",
    }.get(metric, metric)


def _resolve_status_context(snap, history, status_cfg):
    metric = status_cfg.get("metric", "exp_util")
    window_minutes = status_cfg.get("window_minutes", 8)
    window_seconds = max(int(window_minutes * 60), 60)
    util = history.get_utilization(snap["name"], window_seconds=window_seconds) if history else None

    current_metrics = {
        "exp_util": _pct(snap["exp_used"], snap["exp_total"]),
        "nb_util": _pct(snap["nb_used"], snap["nb_total"]),
        "total_util": _pct(snap["total_used"], snap["total_quota"]),
    }
    metric_value = util.get(metric, current_metrics.get(metric, 0.0)) if util else current_metrics.get(metric, 0.0)
    return {
        "util": util,
        "metric": metric,
        "metric_value": metric_value,
        "current_metrics": current_metrics,
        "style": _status_style(metric_value, status_cfg),
    }


def build_status_message(snapshots, history, status_cfg, alert_cfg=None):
    """Build a compact SeaTalk status message with utilization breakdown."""
    now_str = _now_in_alert_timezone(alert_cfg).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"**GPU Monitor**",
        f"`{now_str}`",
        "---",
    ]

    for index, snap in enumerate(snapshots, start=1):
        name = snap["name"]
        total_used = snap["total_used"]
        total_quota = snap["total_quota"]
        exp_used = snap["exp_used"]
        exp_total = snap["exp_total"]
        nb_used = snap["nb_used"]
        nb_total = snap["nb_total"]

        status_ctx = _resolve_status_context(snap, history, status_cfg)
        util = status_ctx["util"]
        status_style = status_ctx["style"]
        current_total_pct = status_ctx["current_metrics"]["total_util"]
        status_icon = status_style.get("icon", "")
        status_label = _format_status_label(status_style, markdown=True)

        lines.append(_pool_heading(index, status_icon, name, snap.get("zone", ""), status_label))
        lines.append(
            f"   - Current: `{_fmt_gpu(total_used)}/{int(total_quota)}` GPUs ({current_total_pct:.0f}%)"
        )
        lines.append(
            f"   - Experiment: `{_fmt_gpu(exp_used)}/{int(exp_total)}`"
            f" | Notebook: `{_fmt_gpu(nb_used)}/{int(nb_total)}`"
        )

        if util:
            mins = util["window_minutes"]
            if mins >= 60:
                window_label = f"{mins // 60}h{mins % 60:02d}m"
            else:
                window_label = f"{mins}min"
            lines.append(
                f"   - **{window_label} avg: exp {util['exp_util']:.0f}%** | "
                f"nb {util['nb_util']:.0f}% | "
                f"total {util['total_util']:.0f}%"
            )
        lines.append("")

    return "\n".join(lines).rstrip()


def build_console_lines(snapshots, history=None, status_cfg=None):
    """Build compact 3-line console output."""
    status_cfg = status_cfg or {}
    lines = []
    for snap in snapshots:
        def fmt(v):
            return str(int(v)) if v == int(v) else f"{v:.1f}"
        status_ctx = _resolve_status_context(snap, history, status_cfg)
        status_style = status_ctx["style"]
        status_icon = status_style.get("icon", "")
        status_label = _format_status_label(status_style, markdown=False)
        now_total = status_ctx["current_metrics"]["total_util"]
        metric_value = status_ctx["metric_value"]
        metric_name = _metric_display_name(status_ctx["metric"])
        lines.append(
            f"  {status_icon} {status_label} | {snap['name']}: "
            f"{fmt(snap['total_used'])}/{int(snap['total_quota'])}"
            f" (now total {now_total:.0f}%, status {metric_name} avg {metric_value:.0f}%)"
            f" | exp {fmt(snap['exp_used'])}/{int(snap['exp_total'])}"
            f" | nb {fmt(snap['nb_used'])}/{int(snap['nb_total'])}"
        )
    return "\n".join(lines)



# ===================== ALERT SCHEDULE CHECK ======================

def _in_alert_schedule(alert_cfg):
    """Return True if current time falls within the configured alert window.

    Checks timezone-aware weekday and time range from alert.schedule config.
    """
    schedule = alert_cfg.get("schedule")
    if not schedule:
        return True  # no schedule configured -> always allowed

    now_tz = _now_in_alert_timezone(alert_cfg)
    weekday = now_tz.isoweekday()  # Mon=1 .. Sun=7
    allowed_days = schedule.get("weekdays", [1, 2, 3, 4, 5])
    if weekday not in allowed_days:
        return False

    current_time = now_tz.strftime("%H:%M")
    start = schedule.get("start_time", "09:30")
    end = schedule.get("end_time", "19:00")
    return start <= current_time < end


# ===================== ALERT TRIGGER CHECK =======================

def check_alert_trigger(history, pools, alert_cfg):
    """Evaluate whether any pool breaches the alert threshold.

    Returns (should_send: bool, danger_pools: list[str]).
    """
    trigger = alert_cfg.get("trigger", {})
    metric = trigger.get("metric", "exp_util")
    window_hours = trigger.get("window_hours", 3)
    threshold = trigger.get("threshold_pct", 70)
    condition = trigger.get("condition", "any_below")

    window_seconds = int(window_hours * 3600)
    danger_pools = []

    for pool_cfg in pools:
        pool_name = pool_cfg["name"]
        util = history.get_utilization(pool_name, window_seconds=window_seconds)
        if util is None:
            # Not enough data yet — skip this pool
            continue
        value = util.get(metric, 0.0)
        if value < threshold:
            danger_pools.append(pool_name)

    if condition == "any_below":
        return len(danger_pools) > 0, danger_pools
    # Default: same as any_below
    return len(danger_pools) > 0, danger_pools


# ========================= MAIN LOOP =============================

def main():
    _enable_live_logs()

    parser = argparse.ArgumentParser(description="Multi-Project GPU Monitor")
    parser.add_argument("--loop", action="store_true", help="Continuous monitoring")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("--verify", action="store_true",
                        help="Send SeaTalk alert to yourself only (private chat)")
    parser.add_argument("--send-group", action="store_true",
                        help="Send SeaTalk alert to group chat")
    parser.add_argument("--interval", type=int, default=None,
                        help="Poll interval in seconds (default: from config, 60)")
    parser.add_argument("--seatalk-interval", type=int, default=None,
                        help="Alert check interval in seconds (default: from config/alert, 3600)")
    args = parser.parse_args()

    # Load config
    cfg = load_monitor_config()
    pools = cfg["pools"]
    st_cfg = cfg["seatalk"]
    gs_cfg = cfg["gsheet"]
    intervals = cfg["intervals"]
    status_cfg = cfg["status"]
    alert_cfg = cfg.get("alert", {})

    poll_interval = args.interval or intervals.get("poll_seconds", 60)
    # Alert check interval: CLI override > alert config > intervals config > 3600
    seatalk_interval = (
        args.seatalk_interval
        or alert_cfg.get("check_interval_seconds")
        or intervals.get("seatalk_seconds", 3600)
    )

    # AIS auth
    ais_token, ais_host = load_ais_auth()
    print(f"[Init] AIS OK ({ais_host})")

    # SeaTalk auth (if sending)
    st_creds = None
    verify_code = None
    if not args.dry_run and (args.verify or args.send_group):
        st_creds = load_seatalk_credentials()
        st_token = seatalk_get_token(st_creds["app_id"], st_creds["app_secret"])
        if not st_token:
            sys.exit("[ERROR] Could not obtain SeaTalk token.")
        print("[Init] SeaTalk token OK")

        if args.verify:
            verify_code = seatalk_resolve_email(st_cfg["verify_email"], st_token)
            if verify_code:
                print(f"[Init] Verify mode: -> {st_cfg['verify_email']} ({verify_code})")
            else:
                sys.exit(f"[ERROR] Could not resolve {st_cfg['verify_email']}")

    # In-memory history — keep at least alert window + margin
    alert_window_seconds = int(alert_cfg.get("trigger", {}).get("window_hours", 3) * 3600)
    status_window_seconds = max(int(status_cfg.get("window_minutes", 180) * 60), 60)
    history_window = max(alert_window_seconds, status_window_seconds, 21600)
    history = MemoryHistory(max_window_seconds=history_window)

    last_alert_check = 0  # epoch of last alert evaluation

    def do_cycle():
        nonlocal last_alert_check

        now = time.time()
        now_display = _now_in_alert_timezone(alert_cfg)
        ts_str = now_display.strftime("%Y-%m-%dT%H:%M:%S")

        # Fetch
        snapshots = fetch_all_snapshots(ais_host, ais_token, pools)

        # In-memory history
        if not args.dry_run:
            history.add(snapshots)

        # Console output
        ts_display = now_display.strftime("%H:%M:%S")
        print(
            f"[{ts_display}]\n"
            f"{build_console_lines(snapshots, history if not args.dry_run else None, status_cfg)}"
        )

        if args.dry_run:
            return

        # JSONL
        try:
            write_jsonl(snapshots, ts_str)
        except Exception as e:
            print(f"[WARN] JSONL write failed: {e}", file=sys.stderr)

        # GSheet
        try:
            write_gsheet(
                gs_cfg["spreadsheet_id"], snapshots, ts_str,
                pools_cfg=pools,
                retention_days=gs_cfg.get("retention_days", 7),
                now_dt=now_display,
            )
        except Exception as e:
            print(f"[WARN] GSheet write failed: {e}", file=sys.stderr)

        # SeaTalk conditional alert (only within configured schedule)
        if (args.verify or args.send_group) and (now - last_alert_check >= seatalk_interval):
            last_alert_check = now
            if not _in_alert_schedule(alert_cfg):
                print(f"[SeaTalk] Outside alert schedule, skipped.")
            else:
                should_send, danger_pools = check_alert_trigger(history, pools, alert_cfg)
                if should_send:
                    try:
                        st_token = seatalk_get_token(
                            st_creds["app_id"], st_creds["app_secret"])
                        if st_token:
                            msg = build_status_message(snapshots, history, status_cfg, alert_cfg)
                            if args.verify and verify_code:
                                ok = seatalk_send_user(verify_code, msg, st_token)
                                print(f"[SeaTalk] Alert sent (verify): {'OK' if ok else 'FAIL'}"
                                      f" | danger: {danger_pools}")
                            elif args.send_group:
                                ok = seatalk_send_group(st_cfg["group_id"], msg, st_token)
                                print(f"[SeaTalk] Alert sent (group): {'OK' if ok else 'FAIL'}"
                                      f" | danger: {danger_pools}")
                    except Exception as e:
                        print(f"[WARN] SeaTalk send failed: {e}", file=sys.stderr)
                else:
                    print(f"[SeaTalk] All pools OK, no alert sent.")
    
    # Execute
    if not args.loop:
        do_cycle()
        return
    
    mode = "VERIFY" if args.verify else ("GROUP" if args.send_group else "LOG-ONLY")
    print(f"PID: {os.getpid()}")
    trigger_desc = alert_cfg.get("trigger", {})
    print(
        f"[Monitor] Loop started (mode={mode}, poll={poll_interval}s, "
        f"alert_check={seatalk_interval}s, "
        f"trigger={trigger_desc.get('metric','exp_util')}<{trigger_desc.get('threshold_pct',70)}% "
        f"over {trigger_desc.get('window_hours',3)}h)"
    )
    
    while True:
        try:
            do_cycle()
        except KeyboardInterrupt:
            print("\n[Monitor] Stopped.")
            break
        except Exception as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            traceback.print_exc()
    
        try:
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\n[Monitor] Stopped.")
            break
    
    
if __name__ == "__main__":
    main()
    
"""
# Foreground (conditional alert to self):
python3 aigc_gpu_alert.py --loop --verify
    
# Background (conditional alert to group):
(cd /Users/youwei.wang/Documents/PythonProject/seatalk-bot \
&& nohup python3 aigc_gpu_alert.py \
    --loop \
    --send-group > aigc_gpu_alert.log 2>&1 & \
echo "Started PID: $!")
    
# Check PID:
head -3 aigc_gpu_alert.log | grep PID
    
# Kill:
kill <pid>
"""
    
