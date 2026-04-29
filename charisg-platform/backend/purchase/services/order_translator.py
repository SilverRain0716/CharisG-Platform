"""
order_translator.py — 쿠팡 주문의 고객 이름·주소를 영문으로 변환.

Phase B: Gemini/Claude 기반 LLM 번역. translation_cache 재사용(같은 이름/주소는 1회만 호출).

- 이름: 로마자 (Revised Romanization 표기 선호)
- 주소: 영문 주소 표기 (한국 도로명/지번 → English with standard ordering)

triggered by coupang_order_poller (신규 주문마다 백그라운드).
또는 /api/pa/orders/{id}/translate 수동 트리거.
"""
import logging
from typing import Optional

from backend.purchase.database import get_db, get_db_hot
from backend_shared.ai.service import translate_text

logger = logging.getLogger(__name__)


NAME_CONTEXT = (
    "Korean personal name. Output only the romanized English name (Revised Romanization), "
    "no explanation. For surname+given-name format use 'Given-Name Surname' (Western order). "
    "Example: 배세희 → Bae Se-hee."
)

ADDRESS_CONTEXT = (
    "Korean postal address. Convert to standard English format suitable for Amazon ship-to. "
    "Order: [building/unit], [road-name/number], [district/si/gu], [province], [postal-code]. "
    "Romanize apartment/building names. Do not translate proper nouns literally. "
    "Output only the English address, no explanation. Example: "
    "'전북특별자치도 군산시 수송동로 20 한라비발디 2차 204동 1401호 (54099)' → "
    "'204-1401, Hanra Vivaldi 2cha, 20 Susong-ro, Gunsan-si, Jeollabuk-do, 54099'"
)


async def translate_order(order_id: int) -> dict:
    """주문 1건의 이름·주소를 번역하고 orders 테이블 업데이트.

    반환: {"ok": bool, "name_en": str, "address_en": str, "error": str | None}
    """
    with get_db_hot() as conn:
        row = conn.execute(
            "SELECT customer_name, address, translation_status FROM orders WHERE id=?",
            (order_id,),
        ).fetchone()
    if not row:
        return {"ok": False, "error": "order not found"}

    ko_name = (row["customer_name"] or "").strip()
    ko_addr = (row["address"] or "").strip()
    if not ko_name and not ko_addr:
        _set_status(order_id, "done", name_en="", address_en="")
        return {"ok": True, "name_en": "", "address_en": ""}

    try:
        name_res = await translate_text(ko_name, source_lang="ko", target_lang="en", context=NAME_CONTEXT) if ko_name else {"translated": ""}
        addr_res = await translate_text(ko_addr, source_lang="ko", target_lang="en", context=ADDRESS_CONTEXT) if ko_addr else {"translated": ""}

        name_en = (name_res.get("translated") or "").strip()
        addr_en = (addr_res.get("translated") or "").strip()

        # 변환 결과가 원문과 같으면 실패 간주 (LLM이 실패시 입력을 그대로 반환하는 fallback 존재).
        failed_name = ko_name and (not name_en or name_en == ko_name)
        failed_addr = ko_addr and (not addr_en or addr_en == ko_addr)
        if failed_name or failed_addr:
            _set_status(
                order_id, "error",
                name_en=name_en if not failed_name else None,
                address_en=addr_en if not failed_addr else None,
            )
            return {"ok": False, "name_en": name_en, "address_en": addr_en, "error": "LLM 번역 실패"}

        _set_status(order_id, "done", name_en=name_en, address_en=addr_en)
        logger.info("[order-translator] order %d 변환 완료", order_id)
        return {"ok": True, "name_en": name_en, "address_en": addr_en, "error": None}
    except Exception as e:
        logger.exception("[order-translator] order %d 예외", order_id)
        _set_status(order_id, "error")
        return {"ok": False, "error": str(e)[:300]}


def _set_status(
    order_id: int,
    status: str,
    name_en: Optional[str] = None,
    address_en: Optional[str] = None,
) -> None:
    sets = ["translation_status=?"]
    params: list = [status]
    if name_en is not None:
        sets.append("customer_name_en=?")
        params.append(name_en)
    if address_en is not None:
        sets.append("address_en=?")
        params.append(address_en)
    params.append(order_id)
    with get_db_hot() as conn:
        conn.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE id=?",
            params,
        )
