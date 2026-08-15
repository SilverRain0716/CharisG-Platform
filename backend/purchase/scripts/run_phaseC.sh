#!/bin/bash
# Phase C — 물놀이 시트 (사용자 명시 지시: 정기 잡 끝난 후 업로딩). enrich→catchup(AI+쿠팡 리스팅).
# 별도 프로세스 = Gemini limiter 새 9000 budget. 어린이제품(KC)/염소화학/IP완구는 [3]에서 excluded.
set -o pipefail
cd /home/ubuntu/CharisG-Platform/charisg-platform || exit 9
L=/tmp/phaseC.log
PY=.venv/bin/python
WURL="https://docs.google.com/spreadsheets/d/1gfb-aC59bXsFWBIAm5b-qy0jqKxhlgDlbmR5q-sQ3vo/edit"
echo "=================== PHASE C START $(date -u +%FT%TZ) ===================" >> "$L"

ENRICH_PIDS=/tmp/enrich_water.csv $PY -m backend.purchase.scripts.enrich_import_sheet "$WURL" --apply >> "$L" 2>&1
echo "=== STEP-C1 enrich rc=$? $(date -u +%FT%TZ) ===" >> "$L"

$PY -m backend.purchase.scripts.catchup_date --from 2026-05-26 --to 2026-05-28 >> "$L" 2>&1
echo "=== STEP-C2 catchup rc=$? $(date -u +%FT%TZ) ===" >> "$L"

echo "=================== PHASE C COMPLETE $(date -u +%FT%TZ) ===================" >> "$L"
