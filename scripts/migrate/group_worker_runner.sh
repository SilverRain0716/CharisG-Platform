#!/usr/bin/env bash
# 그룹 워커 — pending_groups 큐에서 N개 멀티옵션 등록 (cron). flock 중복방지.
set -euo pipefail
BASE=/home/ubuntu/CharisG-Platform/charisg-platform
DIR=$BASE/scripts/migrate
mkdir -p "$DIR/logs" "$DIR/state"
exec 9>"$DIR/state/group_worker.lock"
if ! flock -n 9; then echo "$(date -u +%FT%TZ) 그룹워커 이미 실행중 — 스킵"; exit 0; fi
cd "$BASE"
set -a; . ./.env 2>/dev/null || true; set +a
N="${1:-4}"   # 회당 처리 그룹 수
LOG="$DIR/logs/group_$(TZ=Asia/Seoul date +%F).log"
echo "=== $(TZ=Asia/Seoul date +%FT%T%z) KST | 그룹워커 N=$N ===" | tee -a "$LOG"
"$BASE/.venv/bin/python" "$DIR/migrate_group.py" worker "$N" 2>&1 | grep -vE "^(WARNING|INFO|DEBUG|httpx)" | tee -a "$LOG"
echo "=== done $(TZ=Asia/Seoul date +%FT%T%z) ===" | tee -a "$LOG"
