#!/usr/bin/env bash
# 구계정→신계정 일별 램프 마이그레이션 러너 (cron 일1회).
# 중도 램프: week1 1000 / week2 2500 / week3+ 3500. requested=False(임시저장만) — 판매요청은 사장님 수동.
set -euo pipefail
BASE=/home/ubuntu/CharisG-Platform/charisg-platform
DIR=$BASE/scripts/migrate
PY=$BASE/.venv/bin/python
LOCK=$DIR/state/ramp.lock
START_FILE=$DIR/state/start_date
mkdir -p "$DIR/logs" "$DIR/state"

# 중복 실행 방지 (이전 실행이 안 끝났으면 스킵)
exec 9>"$LOCK"
if ! flock -n 9; then echo "$(date -u +%FT%TZ) 이미 실행중 — 스킵"; exit 0; fi

# 램프 기준일 (최초 1회 고정; 마이그레이션 개시일 2026-06-28)
if [ ! -f "$START_FILE" ]; then echo "2026-06-28" > "$START_FILE"; fi
START=$(cat "$START_FILE")
TODAY=$(TZ=Asia/Seoul date +%F)
DAYS=$(( ( $(date -d "$TODAY" +%s) - $(date -d "$START" +%s) ) / 86400 ))
WEEK=$(( DAYS / 7 ))
if   [ "$WEEK" -le 0 ]; then N=1000
elif [ "$WEEK" -eq 1 ]; then N=2500
else                        N=3500
fi

LOG=$DIR/logs/$(TZ=Asia/Seoul date +%F).log
echo "=== $(TZ=Asia/Seoul date +%FT%T%z) KST | 경과 ${DAYS}일(week${WEEK}) | 오늘 N=${N} ===" | tee -a "$LOG"

cd "$BASE"
set -a; . ./.env 2>/dev/null || true; set +a
export PA_SKIP_GEMINI=1
"$PY" "$DIR/migrate_old.py" "$N" 2>&1 | grep -vE "^(WARNING|INFO|DEBUG|httpx)" | tee -a "$LOG"

echo "=== done $(TZ=Asia/Seoul date +%FT%T%z) ===" | tee -a "$LOG"
