"""채널 주문 상태 → 우리 current_step 매핑.

쿠팡(placedStatus) / 네이버(productOrderStatus) 의 고객 관점 단계와 우리
공급망 7단계(order_received / amazon_purchase / forwarder / international /
domestic / completed / cancelled) 간 forward-only 매핑.

원칙:
- 매핑 결과가 *현재 단계보다 더 진행된 경우* 에만 advance. 후퇴 없음.
- 채널이 우리 내부 단계(amazon_purchase / forwarder / international) 를 알 리 없으므로,
  채널 상태가 'order_received' 또는 None 이면 우리 내부 단계 그대로 유지.
- 매핑 안 되는 상태는 None 반환 → 호출자는 advance 안 함.

호출 예:
    new_step = map_channel_to_step("coupang", "DELIVERING", current_step="amazon_purchase")
    if new_step:
        advance_step(order_id, new_step, note=f"coupang:DELIVERING")
"""
from typing import Optional

# 우리 단계 순서 (forward 비교용)
# 2026-05-12 단순화: forwarder/international/domestic 3단계 → in_transit 1단계.
# 배대지/국제구간은 자체 추적 시스템이 없어 항상 빈 컬럼이었고, 우리가 채널로부터
# 받는 신호는 "DELIVERING" 부터 = 국내 배송 시작점 = in_transit.
ORDER_STEPS_INDEX = {
    "order_received":     0,
    "amazon_purchase":    1,
    "invoice_registered": 2,  # 송장등록(배송지시) — 송장 입력했으나 아직 배송전 (쿠팡 DEPARTURE)
    "in_transit":         3,
    "completed":          4,
    "cancelled":          5,  # 취소는 별도 — completed 와 같은 종결로 취급
}

# 쿠팡 placedStatus → 우리 step
COUPANG_MAP = {
    "ACCEPT":           "order_received",
    "INSTRUCT":         "amazon_purchase",   # 상품준비중 = 우리 구매대기 단계 (forward-only: order_received→amazon_purchase)
    "DEPARTURE":        "invoice_registered", # 배송지시(송장 입력했으나 배송전) — 구매대기 다음 단계
    "DELIVERING":       "in_transit",     # 배송 중
    "FINAL_DELIVERY":   "completed",
    "PURCHASE_CONFIRM": "completed",      # 구매확정 — completed 유지
    "NONE_TRACKING":    "in_transit",     # 송장 등록됐으나 택배 추적 X — 배송 진행으로 간주
    "CANCEL":           "cancelled",
    "CANCEL_RECEIPT":   "cancelled",
    "CANCEL_DONE":      "cancelled",
    "RETURN_RECEIPT":   "cancelled",      # 반품 = cancelled 통합
    "RETURN_DONE":      "cancelled",
}

# 네이버 productOrderStatus / lastChangedType → 우리 step
SMARTSTORE_MAP = {
    "PAY_WAITING":          "order_received",
    "PAYED":                "order_received",
    "DISPATCHED":           "in_transit",   # 발송처리 = 배송 시작
    "DELIVERING":           "in_transit",
    "DELIVERED":            "completed",
    "PURCHASE_DECIDED":     "completed",
    "CANCELED":             "cancelled",
    "CANCELED_BY_NOPAYMENT":"cancelled",
    "RETURNED":             "cancelled",
    "EXCHANGED":            "cancelled",
    "EXCHANGE_OPTION":      None,           # 옵션 변경 — 단계 변동 없음
    "CLAIM_REJECTED":       None,
    "CLAIM_REQUESTED":      None,
    "ADMIN_CANCELING":      None,
    "COLLECT_DONE":         "cancelled",    # 회수 완료
    "GIFT_RECEIVED":        None,
    "HOPE_DELIVERY_INFO_CHANGED": None,
}


def map_channel_to_step(
    channel: str, channel_status: str, current_step: Optional[str] = None,
) -> Optional[str]:
    """채널 상태 → 우리 current_step (forward-only).

    - 매핑 없으면 None
    - 매핑된 step 이 현재 단계보다 작거나 같으면 None (후퇴/제자리 방지)
    - cancelled 는 어느 단계에서든 진입 가능 (특별 케이스)

    Returns: advance 해야 할 새 step, 또는 None.
    """
    if not channel or not channel_status:
        return None
    table = COUPANG_MAP if channel == "coupang" else SMARTSTORE_MAP if channel == "smartstore" else {}
    target = table.get(channel_status)
    if target is None:
        return None
    # cancelled 는 forward 검사 우회
    if target == "cancelled":
        if current_step == "cancelled":
            return None  # 이미 취소됨
        return "cancelled"
    # forward-only
    if current_step:
        cur_idx = ORDER_STEPS_INDEX.get(current_step, -1)
        new_idx = ORDER_STEPS_INDEX.get(target, -1)
        if new_idx <= cur_idx:
            return None
    return target


__all__ = ["map_channel_to_step", "COUPANG_MAP", "SMARTSTORE_MAP", "ORDER_STEPS_INDEX"]
