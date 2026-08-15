"""
sync_coupang_settlement — 쿠팡 정산(월별 지급 + 주문별 매출) 일1회 수집 잡.

동작: BACKFILL_START_YM(2026-01) ~ 현재월(KST) 의 settlement-histories + revenue-history 수집/upsert.
      이미 수집된 월도 매번 재호출(upsert) — 지급 status(SUBJECT→DONE) / 추가 매출 갱신 목적.

배포:
  systemd 타이머 charisg-coupang-settlement.timer (일1회)
  ExecStart=/home/ubuntu/CharisG-Platform/charisg-platform/.venv/bin/python -m backend.purchase.scripts.sync_coupang_settlement

옵션:
  --start YYYY-MM   시작월 (기본 2026-01)
  --end   YYYY-MM   종료월 (기본 현재월)
"""
from __future__ import annotations

import argparse
import logging
import os

# 단발 스크립트 진입 — load_dotenv 명시 + db_factory 등록 (메모리 규칙).
_ROOT = os.environ.get(
    "CHARISG_ROOT",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")),
)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s coupang-settlement: %(message)s")
logger = logging.getLogger("coupang-settlement")

from backend.purchase import database
from backend_shared.context import register_db_factory

register_db_factory(database.get_db)

from backend.purchase.services import coupang_settlement_service as svc
from backend.purchase.services import naver_settlement_service as nsvc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=svc.BACKFILL_START_YM, help="시작월 YYYY-MM")
    ap.add_argument("--end", default=None, help="종료월 YYYY-MM (기본 현재월)")
    ap.add_argument("--channel", default="all", choices=["all", "coupang", "naver"])
    args = ap.parse_args()

    if args.channel in ("all", "coupang"):
        logger.info("쿠팡 정산 동기화 시작 (start=%s end=%s)", args.start, args.end or "current")
        res = svc.sync(args.start, args.end)
        logger.info("쿠팡 완료: 월 %d개 / settlement %d / revenue %d",
                    res["months"], res["settlement_rows"], res["revenue_rows"])

    if args.channel in ("all", "naver"):
        logger.info("네이버 정산 동기화 시작 (start=%s end=%s)", args.start, args.end or "current")
        nres = nsvc.sync(args.start, args.end)
        logger.info("네이버 완료: 월 %d개 / 예정일 %d / revenue %d",
                    nres["months"], nres["case_dates"], nres["revenue_rows"])


if __name__ == "__main__":
    main()
