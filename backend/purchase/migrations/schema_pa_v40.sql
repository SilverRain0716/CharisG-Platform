-- v40: 네이버(스마트스토어) 정산 — 일별 지급 요약 + 건별 정산 상세 (2026-06-04)
--
-- 출처: 네이버 커머스 API (OAuth2 Bearer, naver_commerce_service 토큰 재사용)
--   - 일별  GET /v1/pay-settle/settle/daily   (startDate,endDate,pageNumber,pageSize≤1000)
--           → {elements:[...], pagination}. settleAmount=정산금액(headline), settleExpectDate=정산예정일.
--   - 건별  GET /v1/pay-settle/settle/case     (searchDate 단일일, periodType, pageNumber, pageSize≤1000)
--           → per-order. settleExpectAmount=정산예정금액, paySettleAmount=결제정산(매출기준).
-- 백필: 2026-01~. 일별에서 정산 발생한 settleExpectDate 만 추출해 건별 조회(낭비 방지).
-- 금액 단위 = KRW(정수). 채널 분리: 쿠팡(coupang_*) 과 컬럼 의미 달라 별도 테이블.

-- ── 일별 지급 요약 (settle/daily) ──────────────────────────────
CREATE TABLE IF NOT EXISTS naver_settlement (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    settle_expect_date          TEXT,                     -- 정산 예정일 (월별 집계 기준)
    settle_basis_start_date     TEXT,                     -- 정산 기준 시작일
    settle_basis_end_date       TEXT,                     -- 정산 기준 종료일
    settle_complete_date        TEXT,                     -- 정산 완료일
    settle_amount               INTEGER,                  -- 정산 금액 (headline)
    pay_settle_amount           INTEGER,                  -- 결제 정산 금액(=정산 기준=매출)
    commission_settle_amount    INTEGER,                  -- 수수료 정산 금액
    benefit_settle_amount       INTEGER,                  -- 혜택 정산 금액
    deduction_restore_amount    INTEGER,                  -- 공제 환급 정산 금액
    pay_holdback_amount         INTEGER,                  -- 지급 보류 금액
    normal_settle_amount        INTEGER,                  -- 일반 정산 금액
    quick_settle_amount         INTEGER,                  -- 빠른정산 금액
    preferential_commission     INTEGER,                  -- 우대 수수료 환급 금액
    settle_method_type          TEXT,                     -- ACCOUNT / CHARGE_AMT
    bank_type                   TEXT,
    depositor_name              TEXT,
    account_no                  TEXT,
    merchant_id                 TEXT,
    merchant_name               TEXT,
    synced_at                   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(settle_expect_date, settle_basis_start_date, settle_basis_end_date,
           settle_method_type, bank_type)
);

CREATE INDEX IF NOT EXISTS idx_naver_settlement_expect
    ON naver_settlement(settle_expect_date);

-- ── 건별 정산 상세 (settle/case) ───────────────────────────────
CREATE TABLE IF NOT EXISTS naver_revenue (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_order_id    TEXT NOT NULL,                    -- 상품주문번호(없으면 orderId 대체)
    order_id            TEXT,
    product_order_type  TEXT,                             -- PROD_ORDER / DELIVERY / REFUND / ...
    settle_type         TEXT,                             -- NORMAL_SETTLE_ORIGINAL / *_CANCEL / ...
    settle_basis_date   TEXT,                             -- 정산 기준일
    settle_expect_date  TEXT,                             -- 정산 예정일 (월별 정렬 기준)
    settle_complete_date TEXT,
    pay_date            TEXT,                             -- 결제일
    product_id          TEXT,
    product_name        TEXT,
    purchaser_name      TEXT,
    pay_settle_amount   INTEGER,                          -- 결제 정산(매출 기준)
    commission_amount   INTEGER,                          -- 총 수수료(npay관리+매출연동+무이자할부 합)
    benefit_settle_amount INTEGER,                        -- 혜택 정산 금액
    settle_expect_amount INTEGER,                         -- 정산 예정 금액 (headline)
    merchant_id         TEXT,
    synced_at           TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(product_order_id, settle_type, settle_basis_date)
);

CREATE INDEX IF NOT EXISTS idx_naver_revenue_expect
    ON naver_revenue(settle_expect_date);
CREATE INDEX IF NOT EXISTS idx_naver_revenue_basis
    ON naver_revenue(settle_basis_date);
CREATE INDEX IF NOT EXISTS idx_naver_revenue_order
    ON naver_revenue(order_id);
