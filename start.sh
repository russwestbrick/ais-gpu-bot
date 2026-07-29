#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
nohup python3 -u aigc_gpu_alert.py \
    --loop \
    --verify \
    --seatalk-interval 60 \
    > aigc_gpu_alert.log 2>&1 &
echo "$!" > pid.txt
echo "Started PID: $(cat pid.txt)"
