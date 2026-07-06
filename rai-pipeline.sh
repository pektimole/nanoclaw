#!/bin/bash
# rai-pipeline.sh — RAI threat-research pipeline. OL-325.
#   collect (every 6h): crawl HN+Reddit -> process review queue -> export -> rebake artifact
#   produce (daily):    cluster -> enrich (novelty/layer/cards) -> export -> rebake artifact
# Producer is daily so pattern IDs stay stable across a human-review window.
set -u
MODE="${1:-collect}"
LOG=/home/tim/nanoclaw/logs/rai-pipeline.log
cd /home/tim/nanoclaw || exit 1
echo "=== RAI pipeline [$MODE] $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG"

# Collect (both modes): fetch + classify, apply Tim's queued reviews, export
python3 rai-poc2.py --crawl >> "$LOG" 2>&1
if grep -q "SERPER_API_KEY" /home/tim/nanoclaw/.env 2>/dev/null; then
  python3 rai-reddit-research.py --search >> "$LOG" 2>&1
fi

if [ "$MODE" = "produce" ]; then
  # OL-412 hard-pause: touch .produce-paused to freeze pattern IDs during a promote session
  if [ -f /home/tim/nanoclaw/.produce-paused ]; then
    echo "[$(date -u +%H:%M:%SZ)] produce SKIPPED: .produce-paused present" >> "$LOG"
  else
    # Daily: rebuild un-reviewed patterns from the full pending pool + enrich
    python3 rai-producer.py --all >> "$LOG" 2>&1
  fi
fi

python3 rai-poc2.py --export-json >> "$LOG" 2>&1
python3 build_artifact.py >> "$LOG" 2>&1
echo "=== pipeline [$MODE] done $(date -u +%H:%M:%SZ) ===" >> "$LOG"
