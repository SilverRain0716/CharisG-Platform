#!/bin/bash
# Phase B — 채칼/슬라이서 시트 (사용자 명시 승인). enrich(SP-API)→catchup(AI+쿠팡 리스팅).
# enrich 는 AI 불필요. catchup 은 독립 프로세스 = limiter 새 9000 budget.
# blade(수입금지)/전동KC/방검장갑 은 [3] coupang upload 단계에서 clean_policy 로 excluded.
set -o pipefail
cd /home/ubuntu/CharisG-Platform/charisg-platform || exit 9
L=/tmp/phaseB.log
PY=.venv/bin/python
KURL="https://docs.google.com/spreadsheets/d/1kNc9J0e5pxudrGviVp6fm-djazk-95-yYbAkaQzS6TU/edit"
echo "=================== PHASE B START $(date -u +%FT%TZ) ===================" >> "$L"

ENRICH_PIDS=/tmp/enrich_kitchen.csv $PY -m backend.purchase.scripts.enrich_import_sheet "$KURL" --apply >> "$L" 2>&1
echo "=== STEP-B1 enrich rc=$? $(date -u +%FT%TZ) ===" >> "$L"

$PY -m backend.purchase.scripts.catchup_date --from 2026-05-26 --to 2026-05-28 >> "$L" 2>&1
echo "=== STEP-B2 catchup rc=$? $(date -u +%FT%TZ) ===" >> "$L"

echo "=================== PHASE B COMPLETE $(date -u +%FT%TZ) ===================" >> "$L"
