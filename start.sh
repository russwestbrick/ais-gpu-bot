#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
PID_FILE="$DIR/pid.txt"

# Install dependencies
pip3 install -q shopee-ais -i https://pypi.garenanow.com/simple/
pip3 install -q gspread google-auth
pip3 install tzdata

if [[ -f "$PID_FILE" ]]; then
    old_pid="$(tr -d '[:space:]' < "$PID_FILE")"
    if [[ -n "$old_pid" ]]; then
        old_cmd="$(ps -p "$old_pid" -o command= 2>/dev/null || true)"
        if [[ -n "$old_cmd" ]] && [[ "$old_cmd" == *"aigc_gpu_alert.py"* ]]; then
            kill "$old_pid" 2>/dev/null || true
            echo "Stopped PID: $old_pid"
            sleep 1
        else
            echo "Ignoring stale PID: $old_pid"
        fi
    fi
    rm -f "$PID_FILE"
fi

nohup python3 -u aigc_gpu_alert.py \
    --loop \
    --verify \
    --seatalk-interval 60 \
    > aigc_gpu_alert.log 2>&1 &
echo "$!" > "$PID_FILE"
echo "Started PID: $(cat "$PID_FILE")"
