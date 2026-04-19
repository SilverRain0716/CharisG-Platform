"""
setup_coupang_logistics.py — 쿠팡 출고지/반품지 1회 셋업.

흐름:
1) 네이버 GET addressbooks → RELEASE/REFUND_OR_EXCHANGE 추출
2) 쿠팡 GET으로 .env 기존 코드 검증
   - 동일하면 skip
   - 없거나 다르면 POST 신규 등록
3) 발급된 outboundShippingPlaceCode/returnCenterCode를 stdout + JSON 파일에 출력
4) 사용자가 .env 갱신 후 systemd restart

실행:
    cd /home/ubuntu/CharisG-Platform/charisg-platform
    set -a && source .env && set +a
    python3 -m backend.purchase.scripts.setup_coupang_logistics --user-id <셀러userId> [--dry-run]
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

from backend.purchase.services.naver_commerce_service import get_addressbook_by_type
from backend.purchase.services.coupang_logistics import (
    naver_to_coupang_outbound,
    naver_to_coupang_return,
    list_outbound_shipping_centers,
    list_return_shipping_centers,
    create_outbound_shipping_center,
    create_return_shipping_center,
)
from backend_shared._config import (
    COUPANG_OUTBOUND_SHIPPING_PLACE_CODE,
    COUPANG_RETURN_CENTER_CODE,
)


OUTPUT_PATH = Path.home() / ".coupang_logistics.json"


def _check_existing_outbound(target_code: str) -> bool:
    """쿠팡에 등록된 출고지 중 target_code와 일치하는 게 있는지."""
    if not target_code:
        return False
    res = list_outbound_shipping_centers(page=1, size=50)
    if not res:
        return False
    for c in res.get("content", []) or res.get("data", []):
        code = str(c.get("outboundShippingPlaceCode", "") or c.get("shippingPlaceCode", ""))
        if code == str(target_code):
            return True
    return False


def _check_existing_return(target_code: str) -> bool:
    if not target_code:
        return False
    res = list_return_shipping_centers(page=1, size=50)
    if not res:
        return False
    for c in res.get("content", []) or res.get("data", []):
        code = str(c.get("returnCenterCode", ""))
        if code == str(target_code):
            return True
    return False


def setup_outbound(user_id: str, dry_run: bool = False) -> str:
    logger.info("─" * 60)
    logger.info("[OUTBOUND] 출고지 셋업 시작")

    naver_entry = get_addressbook_by_type("RELEASE")
    if not naver_entry:
        logger.error("네이버에서 RELEASE(출고지) 주소록을 찾을 수 없음")
        return ""
    logger.info(f"  네이버 출고지: addressBookNo={naver_entry['addressBookNo']}, name={naver_entry['name']}")
    logger.info(f"  주소: {naver_entry.get('address', '')}")

    if _check_existing_outbound(COUPANG_OUTBOUND_SHIPPING_PLACE_CODE):
        logger.info(f"  ✓ 쿠팡에 이미 등록됨 — code={COUPANG_OUTBOUND_SHIPPING_PLACE_CODE} (skip)")
        return COUPANG_OUTBOUND_SHIPPING_PLACE_CODE

    payload = naver_to_coupang_outbound(naver_entry, user_id=user_id)
    logger.info(f"  쿠팡 페이로드:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")

    if dry_run:
        logger.info("  [dry-run] POST 생략")
        return ""

    res = create_outbound_shipping_center(payload)
    if not res:
        logger.error("  ✗ 출고지 등록 실패 (위 로그 참조)")
        return ""

    code = str(res.get("data", {}).get("outboundShippingPlaceCode") or res.get("data") or "")
    logger.info(f"  ✓ 출고지 등록 완료 — outboundShippingPlaceCode={code}")
    return code


def setup_return(user_id: str, dry_run: bool = False) -> str:
    logger.info("─" * 60)
    logger.info("[RETURN] 반품지 셋업 시작")

    naver_entry = get_addressbook_by_type("REFUND_OR_EXCHANGE")
    if not naver_entry:
        logger.error("네이버에서 REFUND_OR_EXCHANGE(반품지) 주소록을 찾을 수 없음")
        return ""
    logger.info(f"  네이버 반품지: addressBookNo={naver_entry['addressBookNo']}, name={naver_entry['name']}")
    logger.info(f"  주소: {naver_entry.get('address', '')}")

    if _check_existing_return(COUPANG_RETURN_CENTER_CODE):
        logger.info(f"  ✓ 쿠팡에 이미 등록됨 — code={COUPANG_RETURN_CENTER_CODE} (skip)")
        return COUPANG_RETURN_CENTER_CODE

    payload = naver_to_coupang_return(naver_entry, user_id=user_id)
    logger.info(f"  쿠팡 페이로드:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")

    if dry_run:
        logger.info("  [dry-run] POST 생략")
        return ""

    res = create_return_shipping_center(payload)
    if not res:
        logger.error("  ✗ 반품지 등록 실패 (위 로그 참조)")
        return ""

    code = str(res.get("data", {}).get("returnCenterCode") or res.get("data") or "")
    logger.info(f"  ✓ 반품지 등록 완료 — returnCenterCode={code}")
    return code


def main():
    parser = argparse.ArgumentParser(description="쿠팡 출고지/반품지 1회 셋업 (네이버 주소록 자동 복제)")
    parser.add_argument("--user-id", required=True, help="쿠팡 셀러 userId (vendor 운영자 계정)")
    parser.add_argument("--dry-run", action="store_true", help="페이로드 출력만, POST 생략")
    parser.add_argument("--outbound-only", action="store_true")
    parser.add_argument("--return-only", action="store_true")
    args = parser.parse_args()

    out_code = ""
    ret_code = ""

    if not args.return_only:
        out_code = setup_outbound(args.user_id, dry_run=args.dry_run)
    if not args.outbound_only:
        ret_code = setup_return(args.user_id, dry_run=args.dry_run)

    result = {
        "outboundShippingPlaceCode": out_code,
        "returnCenterCode": ret_code,
    }

    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    logger.info("─" * 60)
    logger.info(f"결과 저장: {OUTPUT_PATH}")
    logger.info(json.dumps(result, ensure_ascii=False, indent=2))
    logger.info("")
    logger.info("[다음 단계]")
    logger.info("1) .env 파일 갱신:")
    if out_code:
        logger.info(f"   COUPANG_OUTBOUND_SHIPPING_PLACE_CODE={out_code}")
    if ret_code:
        logger.info(f"   COUPANG_RETURN_CENTER_CODE={ret_code}")
    logger.info("2) systemd 재시작:")
    logger.info("   sudo systemctl restart charisg-pa-api")


if __name__ == "__main__":
    main()
