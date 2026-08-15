-- v41: 고객 문의(inquiry) dedup + 텔레그램 알림용 저장 테이블 (2026-07-21)
--
-- 출처:
--   - 쿠팡 온라인 문의 (onlineInquiries) — 배송/취소/환불/기타 사후 CS
--   - 쿠팡 콜센터 문의 (callcenterInquiries) — 콜센터 인입 사후 등록
--   - 쿠팡 상품 문의 (productInquiries) — 상품 상세 페이지 Q&A
--   - 네이버 상품 문의 (product-questions) — 스마트스토어 상품 Q&A
--
-- 폴러 (cs_inquiry_poller) 가 30분마다 4개 API 조회 → 신규 문의 INSERT +
-- notified=0 인 것 텔레그램 발송 후 notified=1 마킹.
-- UNIQUE(channel, inquiry_type, inquiry_id) 로 재조회 재삽입 방지.

CREATE TABLE IF NOT EXISTS cs_inquiries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel             TEXT NOT NULL,            -- coupang | smartstore
    inquiry_type        TEXT NOT NULL,            -- online | callcenter | product | product_qna
    inquiry_id          TEXT NOT NULL,            -- 채널 고유 ID
    coupang_account     TEXT,                     -- old | new (쿠팡만)
    order_id            TEXT,                     -- 주문 문의인 경우 채널 주문 ID
    product_id          TEXT,                     -- vendorItemId | sellerProductId | productId
    customer_name       TEXT,
    title               TEXT,
    content             TEXT,
    inquiry_status      TEXT,                     -- 답변대기 / 답변완료 등
    created_at          TEXT,                     -- 채널 응답의 문의 접수 시각
    fetched_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notified            INTEGER NOT NULL DEFAULT 0,   -- 텔레그램 발송 여부
    notified_at         TEXT,
    UNIQUE(channel, inquiry_type, inquiry_id)
);

CREATE INDEX IF NOT EXISTS idx_cs_inquiries_channel
    ON cs_inquiries(channel, inquiry_type);
CREATE INDEX IF NOT EXISTS idx_cs_inquiries_notified
    ON cs_inquiries(notified) WHERE notified = 0;
CREATE INDEX IF NOT EXISTS idx_cs_inquiries_fetched
    ON cs_inquiries(fetched_at);
