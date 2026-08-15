# -*- coding: utf-8 -*-
"""상품 1건을 임포트 후단~쿠팡 등록까지 전 구간 돌리며 AI 비용을 원장으로 계량.

단계
  1) AI 처리   run_two_stage_batch  — 이미지 다운로드 / 번역 / SEO / 카테고리
  2) 상세 생성 detail_agent --install — 주제태깅 + 섹션기획/카피 → seo_detail.json
  3) 채널 배정 send_to_channels(coupang)
  4) 쿠팡 등록 _run_coupang_upload_bg — 속성추출 / 검색어 생성 포함

★불변규칙 유지: 판매요청 안 함(임시저장까지). requested=False 경로.
★ai_ledger 를 맨 먼저 설치해 requests.post 를 가로챈다(사고토큰 포함 집계).
"""
import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, str(BASE))

os.environ["AI_LEDGER"] = "/tmp/ai_ledger.jsonl"
Path("/tmp/ai_ledger.jsonl").unlink(missing_ok=True)

import ai_ledger
ai_ledger.install()                       # ★모든 AI 호출 가로채기 시작

from dotenv import load_dotenv
load_dotenv(BASE / ".env", override=True)
from backend.purchase.database import get_db
# ★standalone 실행 시 필수 — 미등록이면 ai_processor S2(번역/SEO/카테고리)가
#   "db_factory가 등록되지 않았습니다" 로 전멸한다(2026-06-02 기록된 함정).
from backend_shared.context import register_db_factory
register_db_factory(get_db)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mark(label):
    n = len(open("/tmp/ai_ledger.jsonl").readlines()) if Path("/tmp/ai_ledger.jsonl").exists() else 0
    print(f"\n{'─'*72}\n▶ {label}   (누적 AI 콜 {n})\n{'─'*72}", flush=True)
    return time.time()


async def main(pid: int, skip_upload: bool, acct: str):
    t0 = time.time()
    with get_db() as c:
        p = c.execute("SELECT asin,title_en,cost_usd,category_path,status,ai_processed_at "
                      "FROM products WHERE id=?", (pid,)).fetchone()
    from backend.purchase.services.coupang_service import active_account
    print(f"★쿠팡 계정: {acct}  (.env 기본값={active_account()})")
    print(f"대상 pid={pid}  asin={p['asin']}")
    print(f"  {(p['title_en'] or '')[:64]}")
    print(f"  cost=${p['cost_usd']}  cat={p['category_path']}  status={p['status']}  ai={p['ai_processed_at']}")

    # ── 1) AI 처리 ────────────────────────────────────
    t = mark("STEP 1  AI 처리 (이미지 다운로드 / 번역 / SEO / 카테고리)")
    from backend.purchase.services.ai_processor import run_two_stage_batch
    job = uuid.uuid4().hex[:12]
    with get_db() as c:
        c.execute("INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
                  "VALUES (?,'ai_detail','pending',1,?)", (job, _now()))
    await run_two_stage_batch(job, [pid])
    with get_db() as c:
        p = c.execute("SELECT title_ko, seo_tags, category_path, ai_processed_at "
                      "FROM products WHERE id=?", (pid,)).fetchone()
    print(f"  title_ko : {(p['title_ko'] or '(없음)')[:56]}")
    print(f"  seo_tags : {(p['seo_tags'] or '(없음)')[:56]}")
    print(f"  category : {p['category_path']}")
    print(f"  소요 {time.time()-t:.0f}초")
    if not p["ai_processed_at"]:
        print("  ✗ AI 처리 실패 — 중단"); return

    # ── 2) 상세 생성 (에이전트) ────────────────────────
    t = mark("STEP 2  상세페이지 에이전트 (주제태깅 + 섹션기획/카피 → 매니페스트)")
    import subprocess
    r = subprocess.run([str(BASE / ".venv/bin/python"),
                        str(BASE / "backend/purchase/scripts/detail_agent.py"),
                        str(pid), "--policy", "overlay", "--install"],
                       cwd=str(BASE), env={**os.environ, "PYTHONPATH": str(BASE),
                                           "AI_LEDGER": "/tmp/ai_ledger.jsonl"},
                       capture_output=True, text=True, timeout=600)
    for ln in r.stdout.splitlines():
        if any(k in ln for k in ("요약", "[", "설치", "매니페스트", "AI ")):
            print("  " + ln.strip())
    if r.returncode != 0:
        print("  ✗ 에이전트 실패:", r.stderr[-400:])
    man = BASE / f"backend/purchase/media/products/{pid}/seo_detail.json"
    print(f"  매니페스트: {'있음 ' + str(len(json.loads(man.read_text()))) + '블록' if man.exists() else '없음'}")
    print(f"  소요 {time.time()-t:.0f}초")

    # ── 3) 채널 배정 ──────────────────────────────────
    t = mark("STEP 3  채널 배정 (send_to_channels)")
    from backend.purchase.services.channel_listing_service import send_to_channels
    from backend.purchase.services.coupang_service import coupang_account

    def _send():
        with coupang_account(acct):          # ★계정 고정 — 미지정 시 .env(old) 로 샌다
            return send_to_channels(pid, ["coupang"])
    res = await asyncio.to_thread(_send)
    print(f"  {res}")
    print(f"  소요 {time.time()-t:.0f}초")

    # ── 4) 쿠팡 등록 ──────────────────────────────────
    if skip_upload:
        mark("STEP 4  쿠팡 업로드 — 건너뜀(--skip-upload)")
    else:
        t = mark("STEP 4  쿠팡 업로드 (속성추출 / 검색어 생성 포함, 임시저장까지)")
        from backend.purchase.routers.coupang import _run_coupang_upload_bg
        from backend.purchase.services.coupang_service import coupang_account
        job2 = uuid.uuid4().hex[:12]
        with get_db() as c:
            c.execute("INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
                      "VALUES (?,'coupang_upload','pending',1,?)", (job2, _now()))
        with coupang_account(acct):          # ★신계정 고정
            await _run_coupang_upload_bg(job2, [pid])
        with get_db() as c:
            lp = c.execute("SELECT status, channel_product_id, error_message, coupang_account "
                           "FROM listings_pa WHERE product_id=? AND channel='coupang'", (pid,)).fetchone()
        if lp:
            print(f"  status={lp['status']}  spid={lp['channel_product_id']}  계정={lp['coupang_account']}")
            if lp["error_message"]:
                print(f"  err={lp['error_message'][:160]}")
        print(f"  소요 {time.time()-t:.0f}초")

    print(f"\n총 소요 {time.time()-t0:.0f}초")
    ai_ledger.report()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int)
    ap.add_argument("--skip-upload", action="store_true")
    ap.add_argument("--account", default="new", choices=["new", "old"])
    a = ap.parse_args()
    asyncio.run(main(a.pid, a.skip_upload, a.account))
