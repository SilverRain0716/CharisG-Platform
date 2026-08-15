"""삭제 이력 조회 헬퍼 — 재등록 방지용.

`deleted_seller_products` 테이블에 이력이 있는 ASIN 이면 등록 차단.
쿠팡 구·신 계정 모두 대상 (같은 ASIN 이 다른 계정에서 삭제됐어도 차단).

사용:
    from backend.purchase.services.delete_history import is_previously_deleted
    blocked, reason = is_previously_deleted(asin)
    if blocked:
        return {"ok": False, "skip": True, "error": f"previously_deleted:{reason}"}
"""
import logging
from typing import Tuple

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)


def is_previously_deleted(asin: str) -> Tuple[bool, str]:
    """ASIN 이 deleted_seller_products 에 있으면 (True, reason).

    반환: (blocked, reason)
        blocked: 재등록 차단해야 하면 True
        reason: 차단 사유 요약 문자열 (로그·error_message 용)

    이력 없거나 asin 이 비어 있으면 (False, "").
    """
    if not asin:
        return False, ""
    asin_norm = asin.strip().upper()
    if not asin_norm:
        return False, ""
    try:
        with get_db() as conn:
            row = conn.execute(
                """SELECT reason, coupang_account, deleted_at, seller_product_id
                   FROM deleted_seller_products
                   WHERE asin=?
                   ORDER BY deleted_at DESC LIMIT 1""",
                (asin_norm,),
            ).fetchone()
    except Exception:
        # 테이블 없거나 DB 락 등 — 정책상 차단 안 함 (개발 초기 안전망)
        logger.exception("[delete_history] 조회 실패 (계속 진행)")
        return False, ""
    if not row:
        return False, ""
    reason = row["reason"] or "unknown"
    acct = row["coupang_account"] or "?"
    when = row["deleted_at"] or ""
    spid = row["seller_product_id"] or ""
    return True, f"{reason} (from {acct} spid={spid} at {when})"


def is_previously_deleted_any(asins: list[str]) -> tuple[bool, str, str]:
    """여러 ASIN 중 하나라도 이력 있으면 (True, matched_asin, reason).
    변형 그룹 등 자식 ASIN 리스트로 검사할 때 사용.
    """
    if not asins:
        return False, "", ""
    seen = set()
    dedup = []
    for a in asins:
        if not a:
            continue
        a_norm = a.strip().upper()
        if a_norm and a_norm not in seen:
            seen.add(a_norm)
            dedup.append(a_norm)
    if not dedup:
        return False, "", ""
    try:
        with get_db() as conn:
            ph = ",".join("?" * len(dedup))
            row = conn.execute(
                f"""SELECT asin, reason, coupang_account, deleted_at, seller_product_id
                    FROM deleted_seller_products
                    WHERE asin IN ({ph})
                    ORDER BY deleted_at DESC LIMIT 1""",
                tuple(dedup),
            ).fetchone()
    except Exception:
        logger.exception("[delete_history] 벌크 조회 실패 (계속 진행)")
        return False, "", ""
    if not row:
        return False, "", ""
    reason = (
        f"{row['reason']} (from {row['coupang_account']} "
        f"spid={row['seller_product_id']} at {row['deleted_at']})"
    )
    return True, row["asin"], reason
