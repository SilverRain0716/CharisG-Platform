#!/bin/bash
# sid 20 복구 오케스트레이터 — 조용한 창에서 nohup 으로 한 번 실행.
# 1) stale sourcing_candidates 삭제(05-23 잔재 4,967)
# 2) sid 20 재promote (3,334 → draft products)
# 3) catchup_date: 05-25~05-27 draft → AI → 쿠팡 리스팅 (11번가 1099는 이미 listed/ai라 자동 skip)
# 새 시트(채칼)는 이 단계 끝난 뒤 별도 Phase B 로 실행 — 여기 포함 안 함.
set -o pipefail
cd /home/ubuntu/CharisG-Platform/charisg-platform || exit 9
L=/tmp/sid20_recovery.log
PY=.venv/bin/python
echo "=================== SID20 RECOVERY START $(date -u +%FT%TZ) ===================" >> "$L"

$PY -m backend.purchase.scripts.clean_stale_candidates --apply >> "$L" 2>&1
echo "=== STEP1 clean_stale rc=$? $(date -u +%FT%TZ) ===" >> "$L"

$PY -m backend.purchase.scripts.repromote_sid20 >> "$L" 2>&1
echo "=== STEP2 repromote rc=$? $(date -u +%FT%TZ) ===" >> "$L"

$PY -m backend.purchase.scripts.catchup_date --from 2026-05-25 --to 2026-05-27 >> "$L" 2>&1
echo "=== STEP3 catchup rc=$? $(date -u +%FT%TZ) ===" >> "$L"

echo "=================== SID20 RECOVERY COMPLETE $(date -u +%FT%TZ) ===================" >> "$L"
