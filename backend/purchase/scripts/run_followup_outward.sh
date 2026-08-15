#!/bin/bash
# 후속 외부(쿠팡) 작업 — 사용자 명시 승인. 순차(쿠팡 동시부하 회피).
# #3 임시저장 137 재승인(request_approval) → #4 검색태그 catch-up(기존 seo_tags PUT @8/s, AI무관)
set -o pipefail
cd /home/ubuntu/CharisG-Platform/charisg-platform || exit 9
L=/tmp/followup_outward.log
PY=.venv/bin/python
echo "=================== FOLLOWUP OUTWARD START $(date -u +%FT%TZ) ===================" >> "$L"

$PY -m backend.purchase.scripts.reapprove_stuck --apply >> "$L" 2>&1
echo "=== #3 reapprove_stuck rc=$? $(date -u +%FT%TZ) ===" >> "$L"

$PY -m backend.purchase.scripts.backfill_coupang_search_tags --limit 30000 >> "$L" 2>&1
echo "=== #4 search_tags_backfill rc=$? $(date -u +%FT%TZ) ===" >> "$L"

echo "=================== FOLLOWUP OUTWARD COMPLETE $(date -u +%FT%TZ) ===================" >> "$L"
