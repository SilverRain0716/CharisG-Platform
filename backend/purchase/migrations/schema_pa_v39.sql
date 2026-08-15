-- v39: 쿠팡 정산(settlement) — 월별 지급 요약 + 주문별 매출 상세 (2026-06-04)
--
-- 출처: 쿠팡 OPEN API 2종 (HMAC-SHA256, coupang_service._signature 재사용)
--   - 매출내역  GET /v2/providers/openapi/apis/api/v1/revenue-history
--               (vendorId, recognitionDateFrom/To ≤31일, token, maxPerPage≤50 / hasNext·nextToken 페이징)
--   - 지급내역  GET /v2/providers/marketplace_openapi/apis/api/v1/settlement-histories
--               (revenueRecognitionYearMonth=YYYY-MM)
-- 동기화: scripts/sync_coupang_settlement.py (systemd: charisg-coupang-settlement.timer, 일1회)
-- 백필: 2026-01 ~ 현재. 금액 단위 = KRW(정수).

-- ── 월별 지급 요약 (settlement-histories) ──────────────────────────
-- 한 인식월(revenueRecognitionYearMonth)에 settlementType별 여러 지급 row 가능 (WEEKLY 4 + MONTHLY 1 등).
CREATE TABLE IF NOT EXISTS coupang_settlement (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    revenue_recognition_ym      TEXT NOT NULL,            -- '2026-01' (인식월)
    settlement_type             TEXT,                     -- MONTHLY / WEEKLY / ADDITIONAL / RESERVE
    settlement_date             TEXT,                     -- 지급(예정)일
    recognition_date_from       TEXT,
    recognition_date_to         TEXT,
    total_sale                  INTEGER,                  -- 매출합계(취소·차감 제외)
    service_fee                 INTEGER,                  -- 판매수수료(+우대수수료 보전)
    settlement_target_amount    INTEGER,                  -- 정산대상금액(매출-수수료)
    settlement_amount           INTEGER,                  -- 정산액(주1회70% / 월1회100%)
    last_amount                 INTEGER,                  -- 최종액(지급유보분)
    pending_released_amount     INTEGER,                  -- 유보(해제)금액
    final_amount                INTEGER,                  -- 최종 지급(예정)액
    status                      TEXT,                     -- DONE / SUBJECT
    synced_at                   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(revenue_recognition_ym, settlement_type, settlement_date)
);

CREATE INDEX IF NOT EXISTS idx_coupang_settlement_ym
    ON coupang_settlement(revenue_recognition_ym);
CREATE INDEX IF NOT EXISTS idx_coupang_settlement_date
    ON coupang_settlement(settlement_date);

-- ── 주문별 매출 상세 (revenue-history) ─────────────────────────────
-- 같은 주문이 SALE/REFUND 로 나뉘어 여러 인식일에 잡힐 수 있음 → (order_id, sale_type, recognition_date) UNIQUE.
CREATE TABLE IF NOT EXISTS coupang_revenue (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id            TEXT NOT NULL,
    sale_type           TEXT,                             -- SALE / REFUND
    sale_date           TEXT,                             -- 결제완료일
    recognition_date    TEXT,                             -- 매출인식일(배송+7d 또는 구매확정)
    settlement_date     TEXT,                             -- 정산(예정)일
    sale_price          INTEGER,                          -- 판매가(수량 포함 합)
    service_fee         INTEGER,                          -- 판매수수료
    settlement_amount   INTEGER,                          -- 수수료 차감 후 정산액
    delivery_fee        INTEGER,                          -- 배송비 amount (상세는 items_json)
    items_json          TEXT,                             -- items[] 원본 (수량/단가/수수료 분해)
    synced_at           TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(order_id, sale_type, recognition_date)
);

CREATE INDEX IF NOT EXISTS idx_coupang_revenue_recognition
    ON coupang_revenue(recognition_date);
CREATE INDEX IF NOT EXISTS idx_coupang_revenue_settlement_date
    ON coupang_revenue(settlement_date);
CREATE INDEX IF NOT EXISTS idx_coupang_revenue_order
    ON coupang_revenue(order_id);
