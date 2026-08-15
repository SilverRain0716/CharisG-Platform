"""elevenst_account_caps.py — 11번가 계정별 능력치(capability) 조회.

왜 코드가 아니라 DB 를 보나
---------------------------
11번가는 계정이 **글로벌 셀러인지**에 따라 할 수 있는 일이 갈린다(2026-08-11 실측).

  글로벌 셀러(신/스카이포트)
    · `해외직구 > …` 카테고리 등록 가능
    · `forAbrdBuyClf`(해외구매대행 여부)·`abrdBuyPlace`(구입처) 필수

  일반 셀러(구/카리스G)
    · 해외직구 카테고리 → "글로벌 회원만 등록할 수 있습니다" 로 거부
    · 위 두 필드를 넣으면 안 됨(문서: 일반 셀러인 경우 생략)

구계정은 글로벌 전환을 신청해 둔 상태다. 승인이 나면 코드를 고치는 게 아니라
`seller_accounts.capabilities` 의 `global_seller` 를 true 로 바꾸기만 하면 된다.
분기를 코드에 박으면 승인 시점에 배포가 필요해지고, 그 사이 잘못된 페이로드가 나간다.
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 해외직구 계열 말단은 480개(전체 12,314 중 4%). 대부분 건강식품·영양제다.
GLOBAL_ONLY_PREFIX = "해외직구"


def _db():
    from backend.purchase.database import get_db
    return get_db()


def caps(account: str, platform: str = "elevenst") -> dict:
    """계정 능력치. 행이 없거나 JSON 이 깨졌으면 빈 dict."""
    try:
        with _db() as conn:
            r = conn.execute(
                "SELECT capabilities FROM seller_accounts WHERE platform=? AND account_key=?",
                (platform, account)).fetchone()
    except Exception as e:
        logger.warning("[11st-caps] 조회 실패 %s: %s", account, e)
        return {}
    if not r or not r["capabilities"]:
        return {}
    try:
        return json.loads(r["capabilities"]) or {}
    except json.JSONDecodeError:
        logger.warning("[11st-caps] capabilities JSON 파손: %s", account)
        return {}


def is_global_seller(account: str) -> bool:
    """★기본값 False. 모르면 '일반 셀러'로 보는 쪽이 안전하다 —
    글로벌 전용 필드를 잘못 넣으면 등록이 거부되지만, 빼면 일반 카테고리엔 올라간다."""
    return bool(caps(account).get("global_seller", False))


def category_allowed(account: str, full_path: str) -> bool:
    """이 계정이 그 카테고리에 등록할 수 있나."""
    if (full_path or "").startswith(GLOBAL_ONLY_PREFIX):
        return is_global_seller(account)
    return True


def candidate_filter_sql(account: str) -> tuple[str, list]:
    """카테고리 후보 검색에 붙일 WHERE 조각.

    ★global_dlv 로는 거르지 않는다. 해외배송 불가 표시(gblDlvYn=N) 카테고리에도
      실제로는 등록이 된다(2026-08-11 실측 — 골프용품 1020704 성공).
      그 조건을 걸면 정답 카테고리가 후보에서 통째로 빠진다.
    """
    if is_global_seller(account):
        return "", []
    return " AND full_path NOT LIKE ?", [f"{GLOBAL_ONLY_PREFIX}%"]


def payload_overrides(account: str) -> dict:
    """계정별 페이로드 차이.

    반환: {"include": {필드: 값}, "omit": [필드…]}
    """
    if is_global_seller(account):
        return {"include": {"forAbrdBuyClf": "01", "abrdBuyPlace": "D"}, "omit": []}
    # 일반 셀러 — 글로벌 전용 필드를 넣으면 거부된다
    return {"include": {}, "omit": ["forAbrdBuyClf", "abrdBuyPlace"]}
