# -*- coding: utf-8 -*-
"""텔레그램 알림 — 신규주문 등 이벤트를 Bot API로 발송 (2026-07-09).
env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. 둘 중 하나라도 없으면 조용히 no-op.
발송 실패는 절대 호출부(주문수집)를 깨지 않도록 전부 삼킨다."""
import os
import logging
import requests

logger = logging.getLogger(__name__)

DASHBOARD_BASE = os.environ.get("DASHBOARD_BASE", "https://wongbigo.com/purchase")
_CH_KO = {"coupang": "쿠팡", "smartstore": "스마트스토어", "naver": "네이버"}


def _channel_label(channel: str | None, account: str | None = None) -> str:
    """채널 표시 라벨. 쿠팡은 account 있으면 '쿠팡(구)' / '쿠팡(신)'."""
    ch = _CH_KO.get(channel or "", channel or "-")
    if channel == "coupang" and account:
        suffix = {"old": "구", "new": "신"}.get(account, account)
        return f"{ch}({suffix})"
    return ch


def _cfg():
    return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def send_message(text: str, parse_mode: str | None = None) -> bool:
    """TELEGRAM_CHAT_ID(쉼표로 여러 개 = 여러 수신자/그룹) 전원에게 발송. 하나라도 성공하면 True."""
    token, chat_raw = _cfg()
    if not (token and chat_raw):
        return False
    chat_ids = [c.strip() for c in str(chat_raw).split(",") if c.strip()]
    any_ok = False
    for chat_id in chat_ids:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
                timeout=10,
            )
            if r.status_code >= 400:
                logger.warning("[telegram] send %s(%s): %s", r.status_code, chat_id, r.text[:150])
            else:
                any_ok = True
        except Exception as e:
            logger.warning("[telegram] send 예외(%s): %s", chat_id, e)
    return any_ok


def notify_new_order(order_id, channel, product_name, brand,
                     customer_name, quantity, sale_price_krw, link_url=None,
                     coupang_account=None) -> bool:
    """신규주문 알림 + 링크. link_url 주면 그걸(토큰 스냅샷 등), 없으면 대시보드 딥링크.
    coupang_account: 쿠팡의 경우 'old'/'new' → 라벨에 (구)/(신) 표시."""
    ch = _channel_label(channel, coupang_account)
    pn = (product_name or "(상품명 미상)").strip()
    if brand:
        pn = f"{pn} ({brand})"
    lines = [f"\U0001F6D2 신규주문 [{ch}]", f"\U0001F4E6 {pn[:80]}"]
    lines.append(f"\U0001F464 {customer_name or '-'} · 수량 {quantity or 1}개")
    try:
        lines.append(f"\U0001F4B0 {int(round(float(sale_price_krw))):,}원")
    except (TypeError, ValueError):
        pass
    lines.append(f"\U0001F449 {link_url or (DASHBOARD_BASE + '/orders/' + str(order_id))}")
    return send_message("\n".join(lines))


def notify_new_inquiry(inquiry_id, channel, inquiry_type, product_name=None,
                       customer_name=None, title=None, content=None,
                       order_id=None, link_url=None, coupang_account=None) -> bool:
    """신규 고객문의 알림 (2026-07-21 추가).

    inquiry_type: online | callcenter | product | product_qna
    coupang_account: 쿠팡의 경우 'old'/'new' → 라벨에 (구)/(신) 표시.
    """
    ch = _channel_label(channel, coupang_account)
    type_map = {
        "online": "온라인문의",
        "callcenter": "콜센터문의",
        "product": "상품문의",
        "product_qna": "상품 Q&A",
    }
    typ = type_map.get(inquiry_type, "문의")
    pn = (product_name or "(상품명 미상)").strip()
    lines = [f"\U0001F4AC 신규 {typ} [{ch}]"]
    lines.append(f"\U0001F4E6 {pn[:80]}")
    if title:
        lines.append(f"제목: {str(title)[:80]}")
    if content:
        c = str(content).strip()
        if c:
            lines.append(f"내용: {c[:200]}")
    if customer_name:
        lines.append(f"\U0001F464 {customer_name}")
    if order_id:
        lines.append(f"주문: {order_id}")
    if link_url:
        lines.append(f"\U0001F449 {link_url}")
    return send_message("\n".join(lines))


def notify_order_cancelled(order_id, channel, product_name=None, brand=None,
                            cancel_reason=None, sale_price_krw=None,
                            cancel_type=None, link_url=None,
                            coupang_account=None) -> bool:
    """주문 취소/반품 알림 (2026-07-21 추가).

    cancel_type: "CANCEL"=결제 즉시 취소, "RETURN"=반품, "CLAIM"=스마트스토어 클레임.
    coupang_account: 쿠팡의 경우 'old'/'new' → 라벨에 (구)/(신) 표시.
    """
    ch = _channel_label(channel, coupang_account)
    tag_map = {"CANCEL": "취소", "RETURN": "반품", "CLAIM": "클레임"}
    tag = tag_map.get(cancel_type, "취소/반품")
    pn = (product_name or "(상품명 미상)").strip()
    if brand:
        pn = f"{pn} ({brand})"
    lines = [f"❌ {tag} [{ch}]", f"\U0001F4E6 {pn[:80]}"]
    if cancel_reason:
        lines.append(f"\U0001F4AC 사유: {str(cancel_reason)[:100]}")
    try:
        lines.append(f"\U0001F4B0 {int(round(float(sale_price_krw))):,}원")
    except (TypeError, ValueError):
        pass
    lines.append(f"\U0001F449 {link_url or (DASHBOARD_BASE + '/orders/' + str(order_id))}")
    return send_message("\n".join(lines))
