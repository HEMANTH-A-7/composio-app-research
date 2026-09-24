#!/usr/bin/env bash
# Runs every stage to completion. Each stage is cached per app, so on a rate/session
# limit we just wait and rerun; only the missing apps are retried.
set -u
cd "$(dirname "$0")/.."
stage_until_done () {
  local s=$1 w=$2
  for attempt in $(seq 1 12); do
    python3 agent/run.py "$s" --workers "$w" 2>&1 | tee -a "data/log_$s.txt"
    n=$(wc -l < "data/$s.jsonl" 2>/dev/null || echo 0)
    [ "$n" -ge 100 ] && return 0
    echo "[$s] $n/100 done, waiting 10 min before retry $attempt"; sleep 600
  done
}
stage_until_done baseline 4
stage_until_done scout 6
python3 agent/run.py ground --workers 10 | tee -a data/log_ground.txt
python3 agent/run.py verify --workers 6 | tee -a data/log_verify.txt
python3 agent/run.py review
