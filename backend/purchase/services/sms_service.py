"""sms_service.py — 솔라피(SOLAPI) 문자 발송.

자동 발송은 하지 않는다. 주문 상세 화면에서 운영자가 본문을 직접 써서 보낸 건만 처리한다.

핵심 제약 (실측 근거):
  - 주문의 customer_phone 은 대부분 050 안심번호(0502 109건 / 0504 50건).
    050 은 국번별로 LMS/MMS 가 막혀 있어 **SMS 단문만** 가능하고, 0508 은 SMS 도 불가.
  - 저장된 번호에 하이픈이 섞여 있다(010-... 12건). 발송 직전 반드시 정규화.
  - SMS 한도는 '45자'가 아니라 EUC-KR 90byte. 한글 2byte / 영문·숫자 1byte.

정책:
  - 기본은 안심번호(safe). 실번호(real)는 통관·주소 오류처럼 안심번호로 해결이 안 되는
    건에만 쓰고, 사유를 남긴다. 실번호는 통관 목적으로 수집한 개인정보라 목적 외 사용을 막기 위함.

EC2 의존: 운영 SOLAPI_API_KEY 는 IP 화이트리스트(52.78.174.31/32)가 걸려 있다.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from backend_shared._config import (
    SOLAPI_API_KEY,
    SOLAPI_API_SECRET,
    SOLAPI_SENDER,
    SOLAPI_SMS_PREFIX,
)
from backend.purchase.database import get_db_hot

logger = logging.getLogger(__name__)

BASE_URL = "https://api.solapi.com"
SEND_PATH = "/messages/v4/send-many/detail"
BALANCE_PATH = "/cash/v1/balance"

SMS_MAX_BYTES = 90       # EUC-KR 기준 단문 한도
LMS_MAX_BYTES = 2000     # 장문 한도
DEDUP_WINDOW_SEC = 60    # 동일 주문·동일 본문 재발송 차단 창

# 실번호 사용이 허용되는 사유 — 안심번호로 해결 불가능한 것만.
REAL_PHONE_REASONS = {
    "customs_error": "개인통관부호 오류",
    "address_unclear": "주소 불명확",
    "safe_number_failed": "안심번호 발송 실패",
}

# 050 국번별 발송 가능 여부. 값은 LMS 가능 여부.
SAFE_PREFIX_LMS_OK = {
    "0502": False,
    "0503": False,   # 일부 국번만 가능 — 보수적으로 불가 처리
    "0504": False,   # 일부 국번만 가능 — 보수적으로 불가 처리
    "0505": True,
    "0506": False,
    "0507": False,
}
BLOCKED_PREFIXES = {"0508"}   # SMS 조차 불가


class SmsError(Exception):
    """발송 전 검증 실패 — 호출부에서 422 로 변환한다."""


# ──────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────

def normalize_phone(raw: Optional[str]) -> str:
    """하이픈·공백 등 숫자 아닌 문자 제거. 저장값을 믿지 않는다."""
    if not raw:
        return ""
    return "".join(ch for ch in str(raw) if ch.isdigit())


def euckr_len(text: str) -> int:
    """EUC-KR 기준 byte 길이. 범위를 벗어난 문자가 있으면 SmsError.

    솔라피는 EUC-KR 범위 밖 문자를 상태코드 1029 로 거부한다(이모지 등).
    """
    try:
        return len(text.encode("euc-kr"))
    except UnicodeEncodeError as e:
        bad = text[e.start:e.end]
        raise SmsError(f"사용할 수 없는 문자가 있습니다: {bad!r} (이모지·특수문자 불가)")


def infer_number_type(phone: str) -> str:
    """정규화된 번호로 safe/real 판별."""
    return "safe" if phone.startswith("050") else "real"


def _auth_header() -> str:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    salt = secrets.token_hex(16)
    signature = hmac.new(
        SOLAPI_API_SECRET.encode(), (date + salt).encode(), hashlib.sha256
    ).hexdigest()
    return (
        f"HMAC-SHA256 apiKey={SOLAPI_API_KEY}, date={date}, "
        f"salt={salt}, signature={signature}"
    )


def _request(method: str, path: str, json_body: Optional[dict] = None) -> dict:
    if not (SOLAPI_API_KEY and SOLAPI_API_SECRET):
        raise SmsError("솔라피 API 키가 설정되지 않았습니다 (.env SOLAPI_API_KEY)")
    resp = requests.request(
        method,
        BASE_URL + path,
        headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
        json=json_body,
        timeout=15,
    )
    try:
        data = resp.json()
    except ValueError:
        raise SmsError(f"솔라피 응답 파싱 실패 (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        msg = data.get("errorMessage") or data.get("message") or str(data)
        raise SmsError(f"솔라피 오류 (HTTP {resp.status_code}): {msg}")
    return data


# ──────────────────────────────────────────────
# 검증
# ──────────────────────────────────────────────

def validate(
    to_raw: str,
    number_type: str,
    reason: Optional[str],
    text: str,
) -> dict:
    """발송 전 전량 검증. 통과하면 {phone, msg_type, byte_len, body} 반환."""
    if not SOLAPI_SENDER:
        raise SmsError("발신번호가 설정되지 않았습니다 (.env SOLAPI_SENDER)")

    body = (SOLAPI_SMS_PREFIX + text) if SOLAPI_SMS_PREFIX else text
    body = body.strip()
    if not body:
        raise SmsError("본문이 비어 있습니다")

    phone = normalize_phone(to_raw)
    if len(phone) < 9:
        raise SmsError(f"수신번호 형식이 올바르지 않습니다: {to_raw!r}")

    actual_type = infer_number_type(phone)
    if actual_type != number_type:
        raise SmsError(
            f"번호 종류 불일치 — 요청은 {number_type} 인데 실제 번호는 {actual_type} 입니다"
        )

    if number_type == "real":
        if reason not in REAL_PHONE_REASONS:
            raise SmsError(
                "실번호 발송은 사유가 필요합니다 "
                f"({', '.join(REAL_PHONE_REASONS)})"
            )

    byte_len = euckr_len(body)

    if number_type == "safe":
        prefix = phone[:4]
        if prefix in BLOCKED_PREFIXES:
            raise SmsError(f"{prefix} 안심번호는 문자 수신이 불가합니다. 실번호를 사용하세요.")
        # 안심번호는 LMS 가 사실상 막혀 있으므로 단문 한도를 넘기면 발송하지 않는다.
        if byte_len > SMS_MAX_BYTES:
            raise SmsError(
                f"안심번호는 단문만 가능합니다. {byte_len}/{SMS_MAX_BYTES}byte "
                f"({byte_len - SMS_MAX_BYTES}byte 초과) — 본문을 줄이세요."
            )
        msg_type = "SMS"
    else:
        if byte_len > LMS_MAX_BYTES:
            raise SmsError(f"본문이 너무 깁니다: {byte_len}/{LMS_MAX_BYTES}byte")
        msg_type = "SMS" if byte_len <= SMS_MAX_BYTES else "LMS"

    return {"phone": phone, "msg_type": msg_type, "byte_len": byte_len, "body": body}


def _check_duplicate(order_id: int, body: str) -> None:
    """오클릭으로 같은 문자를 연속 발송하는 사고를 막는다."""
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=9) - timedelta(seconds=DEDUP_WINDOW_SEC)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with get_db_hot() as conn:
        row = conn.execute(
            """SELECT id FROM order_sms
               WHERE order_id=? AND text=? AND status!='failed' AND sent_at >= ?
               LIMIT 1""",
            (order_id, body, cutoff),
        ).fetchone()
    if row:
        raise SmsError(f"{DEDUP_WINDOW_SEC}초 이내에 같은 내용을 이미 보냈습니다 (중복 발송 방지)")


# ──────────────────────────────────────────────
# 발송
# ──────────────────────────────────────────────

def send_order_sms(
    order_id: int,
    to_raw: str,
    number_type: str,
    text: str,
    reason: Optional[str] = None,
    sent_by: Optional[str] = None,
) -> dict:
    """주문 1건에 대한 수동 문자 발송 + 이력 기록."""
    checked = validate(to_raw, number_type, reason, text)
    _check_duplicate(order_id, checked["body"])

    payload = {
        "messages": [
            {
                "to": checked["phone"],
                "from": normalize_phone(SOLAPI_SENDER),
                "text": checked["body"],
                "type": checked["msg_type"],
                # 자동 타입 감지를 끈다. 켜두면 90byte 초과 시 조용히 LMS 로 승격되어
                # 안심번호에서 3031/3050 으로 실패한다.
                "autoTypeDetect": False,
            }
        ],
        "allowDuplicates": False,
    }

    status = "failed"
    status_code = None
    message_id = None
    group_id = None
    error_msg = None

    try:
        data = _request("POST", SEND_PATH, payload)
        group_info = data.get("groupInfo") or {}
        group_id = group_info.get("groupId")
        failed = data.get("failedMessageList") or []
        if failed:
            first = failed[0]
            error_msg = (first.get("error") or {}).get("message") or "발송 실패"
            status_code = first.get("statusCode")
        else:
            status = "sent"
            msgs = data.get("messageList") or []
            if msgs:
                message_id = msgs[0].get("messageId")
                status_code = msgs[0].get("statusCode")
            status_code = status_code or "2000"
    except SmsError:
        raise
    except Exception as e:  # 네트워크 등
        error_msg = str(e)[:300]
        logger.warning(f"[sms] order {order_id} 발송 실패: {e}")

    with get_db_hot() as conn:
        cur = conn.execute(
            """INSERT INTO order_sms
               (order_id, to_number, number_type, reason, text, msg_type, byte_len,
                message_id, group_id, status_code, status, error_msg, sent_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order_id, checked["phone"], number_type, reason, checked["body"],
                checked["msg_type"], checked["byte_len"],
                message_id, group_id, status_code, status, error_msg, sent_by,
            ),
        )
        sms_id = cur.lastrowid

    if status != "sent":
        raise SmsError(error_msg or "발송에 실패했습니다")

    return {
        "ok": True,
        "id": sms_id,
        "to": checked["phone"],
        "msg_type": checked["msg_type"],
        "byte_len": checked["byte_len"],
        "group_id": group_id,
        "message_id": message_id,
    }


def list_order_sms(order_id: int) -> list[dict]:
    with get_db_hot() as conn:
        rows = conn.execute(
            """SELECT id, to_number, number_type, reason, text, msg_type, byte_len,
                      status, status_code, error_msg, sent_by, sent_at
               FROM order_sms WHERE order_id=? ORDER BY id DESC LIMIT 50""",
            (order_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_balance() -> dict:
    """잔액 조회. balance(캐시) 와 point(무료 포인트) 둘 다 발송에 쓰인다."""
    data = _request("GET", BALANCE_PATH)
    balance = float(data.get("balance") or 0)
    point = float(data.get("point") or 0)
    return {
        "balance": balance,
        "point": point,
        "total": balance + point,
        "sender": SOLAPI_SENDER,
    }
