"""쿠팡 미적용 listings 마진대별 쿠폰 catch-up.

apply_coupons_to_listings() 단일 호출. 전체 미적용 listed (마진 ≥10K) 대상.
1회용 수동 트리거 또는 watcher 에서 호출.

실행: python -m backend.purchase.scripts.apply_coupon_catchup
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(_ENV_PATH)

from backend.purchase.services.coupon_apply import apply_coupons_to_listings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("coupon-catchup")


def main() -> None:
    result = apply_coupons_to_listings()
    logger.info(f"=== catch-up 완료 — {result} ===")


if __name__ == "__main__":
    main()
