# -*- coding: utf-8 -*-
"""주문 → 자식(옵션) 상품 확정 폴백 (2026-08-08).

배경
----
옵션 상품 주문은 `listing_options.channel_option_id` (쿠팡 vendorItemId /
네이버 channelProductNo) 로 자식을 찾는다. 그런데 등록 후 이 값을 되받아 저장하는
단계가 빠져 **123,528행 중 48,514행(39%)이 미기록**이었다. 조회가 실패하면
`child_product_id=None` 이 되고, 대시보드는 `child_product_id or product_id` 로
폴백해 **그룹 대표(형제 옵션)** 를 보여준다 → 다른 색/사이즈로 발주되는 오배송.

실제 피해: 주문 1221(스트로우 세트→블랙), 1647(캐스케이드→블랙),
1677(GIR 변형 불일치), 1730(퍼플→블루). 1221·1677 은 이미 발송 완료.

해결
----
채널이 외부 SKU 로 **자식 ASIN 을 그대로 돌려준다**(쿠팡 externalVendorSkuCode,
네이버 optionManageCode). 종전에는 이 값을 "PA-{id}" 정규식에만 쓰고 버렸다.
`listing_options` 가 비어 있어도 이 값으로 자식을 정확히 복원할 수 있다.

★근본 원인(channel_option_id 미기록)은 별건으로 남는다. 이 폴백은 그 상태에서도
  주문이 올바르게 매핑되게 하는 안전망이지, 미기록을 고치는 게 아니다.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 아마존 ASIN: 10자. 우리 카탈로그는 전부 B0 로 시작한다(도서 ISBN 계열은 취급 안 함).
# "PA-12345" 같은 내부 SKU 와 확실히 구분하기 위해 형태를 엄격히 본다.
_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")


def resolve_child_by_sku(external_sku, channel: str = "") -> tuple[int | None, str | None]:
    """외부 SKU(자식 ASIN)로 (child_product_id, child_asin) 확정. 실패하면 (None, None).

    부작용 없음 — 조회만 한다. 호출측은 기존 vendorItemId 조회가 실패했을 때만 쓰면 된다.
    """
    if not external_sku:
        return None, None
    sku = str(external_sku).strip().upper()
    if not _ASIN_RE.match(sku):
        return None, None
    try:
        from backend.purchase.database import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, asin FROM products WHERE asin=? LIMIT 1", (sku,)
            ).fetchone()
        if row:
            logger.info(
                f"[order-child-fallback] {channel} external_sku={sku} → child pid={row['id']} "
                f"(listing_options 미기록 보정)"
            )
            return row["id"], row["asin"]
    except Exception as e:
        logger.warning(f"[order-child-fallback] {channel} sku={sku} 조회 실패: {e}")
    return None, None
