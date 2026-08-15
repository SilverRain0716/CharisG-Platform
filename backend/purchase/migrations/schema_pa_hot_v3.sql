-- hot.db v3 — order_sms: 주문 상세에서 수동 발송한 문자 이력
--
-- 자동 발송은 하지 않는다. 운영자가 주문 상세 화면에서 본문을 직접 작성해 보낸 건만 남는다.
-- number_type 은 '용도별 분리' 정책의 감사 근거 — real(실번호) 은 reason 없이 발송 불가.

CREATE TABLE IF NOT EXISTS order_sms (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,

    to_number    TEXT NOT NULL,              -- 하이픈 제거된 발송 실번호
    number_type  TEXT NOT NULL,              -- safe(안심번호 050) | real(실휴대폰 010)
    reason       TEXT,                       -- number_type='real' 일 때 필수 (통관오류/주소불명/안심번호실패)

    text         TEXT NOT NULL,
    msg_type     TEXT NOT NULL DEFAULT 'SMS',  -- SMS | LMS
    byte_len     INTEGER NOT NULL DEFAULT 0,   -- EUC-KR 기준 본문 길이

    message_id   TEXT,                       -- 솔라피 messageId
    group_id     TEXT,                       -- 솔라피 groupId
    status_code  TEXT,                       -- 4000=수신완료, 2000=접수, 1030=잔액부족 ...
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed
    error_msg    TEXT,

    sent_by      TEXT,                       -- 발송한 운영자 (JWT sub)
    sent_at      TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))
);

CREATE INDEX IF NOT EXISTS idx_order_sms_order ON order_sms(order_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_order_sms_sent  ON order_sms(sent_at DESC);
