"""Discord webhook notifier — 작업 완료/주문/에러 알림.

DISCORD_WEBHOOK_URL 환경변수에 webhook URL 설정 시 동작. 미설정이면 조용히 skip.
실패 시 log 만 남기고 무시 — 알림 실패가 본 작업을 막지 않도록.
"""
import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
TIMEOUT = 5.0  # webhook 5초 안에 응답 안 오면 포기

# 색상 (Discord embed)
COLOR_SUCCESS = 0x2ECC71  # green
COLOR_INFO = 0x3498DB     # blue
COLOR_WARN = 0xF39C12     # orange
COLOR_ERROR = 0xE74C3C    # red
COLOR_NEW = 0x9B59B6      # purple — 주문 등 신규 이벤트


def _post(payload: dict) -> bool:
    """webhook POST. 성공 True. 실패는 False + warning log."""
    if not WEBHOOK_URL:
        return False
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
        if r.status_code >= 400:
            logger.warning(f"Discord webhook 실패: {r.status_code} {r.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"Discord webhook 예외 (무시): {e}")
        return False


def send_embed(
    title: str,
    description: str = "",
    fields: Optional[list[dict]] = None,
    color: int = COLOR_INFO,
) -> bool:
    """Embed 형식 알림 전송.

    fields: [{"name": "...", "value": "...", "inline": True}, ...]
    """
    embed = {
        "title": title[:256],  # Discord limit
        "color": color,
    }
    if description:
        embed["description"] = description[:4096]
    if fields:
        embed["fields"] = fields[:25]  # max 25 fields
    return _post({"embeds": [embed]})


def notify_upload_complete(
    channel: str, success: int, errors: int, total: int, duration_sec: float
) -> None:
    """채널 업로드 일괄 완료 알림."""
    label = "스마트스토어" if channel == "smartstore" else "쿠팡"
    fail_pct = (errors / total * 100) if total else 0
    color = COLOR_SUCCESS if errors == 0 else (COLOR_WARN if fail_pct < 10 else COLOR_ERROR)
    icon = "✅" if errors == 0 else ("⚠️" if fail_pct < 10 else "❌")

    duration_str = f"{int(duration_sec // 60)}분 {int(duration_sec % 60)}초"
    fields = [
        {"name": "성공", "value": f"{success} / {total}", "inline": True},
        {"name": "실패", "value": str(errors), "inline": True},
        {"name": "소요", "value": duration_str, "inline": True},
    ]
    send_embed(f"{icon} {label} 업로드 완료", fields=fields, color=color)


def notify_promote_complete(
    new_count: int, duplicate_count: int, enriched: int, errors: int,
    banned_diet: int = 0,
) -> None:
    """소싱 promote 완료 알림."""
    color = COLOR_SUCCESS if errors == 0 else COLOR_WARN
    fields = [
        {"name": "신규", "value": f"{new_count}건", "inline": True},
        {"name": "중복 skip", "value": f"{duplicate_count}건", "inline": True},
        {"name": "SP-API 보강", "value": f"{enriched}건", "inline": True},
    ]
    if banned_diet:
        fields.append({"name": "약사법 차단", "value": f"{banned_diet}건", "inline": True})
    if errors:
        fields.append({"name": "오류", "value": f"{errors}건", "inline": True})
    send_embed("📋 소싱 promote 완료", fields=fields, color=color)


def notify_swap_complete(requested: int, ok: int, fail: int) -> None:
    """smartstore listings rotation (한도 회전) 완료 알림."""
    color = COLOR_SUCCESS if fail == 0 else COLOR_WARN
    fields = [
        {"name": "요청", "value": f"{requested}건", "inline": True},
        {"name": "성공", "value": f"{ok}건", "inline": True},
    ]
    if fail:
        fields.append({"name": "실패", "value": f"{fail}건", "inline": True})
    send_embed("🔄 smartstore 한도 회전 (영구삭제 swap)", fields=fields, color=color)


# 신규 주문 알림 — rate limit (10초 내 같은 channel 다회 호출 시 묶기)
_last_order_notify: dict[str, float] = {}
_ORDER_NOTIFY_MIN_INTERVAL = 2.0  # 같은 채널 알림 최소 간격(초)


def notify_new_order(
    channel: str, product_name: str, asin: str, option: Optional[str],
    price_krw: int, order_id: str
) -> None:
    """신규 주문 1건 알림. 너무 빠른 연속 호출은 무시(폭주 방지)."""
    now = time.time()
    last = _last_order_notify.get(channel, 0)
    if now - last < _ORDER_NOTIFY_MIN_INTERVAL:
        return
    _last_order_notify[channel] = now

    label = "네이버" if channel == "smartstore" else "쿠팡"
    desc_parts = [
        f"**{product_name}**",
        f"ASIN: `{asin}`" + (f" | 옵션: {option}" if option else ""),
        f"가격: ₩{price_krw:,}",
        f"주문ID: `{order_id}`",
    ]
    send_embed(
        f"🛒 신규 {label} 주문",
        description="\n".join(desc_parts),
        color=COLOR_NEW,
    )
