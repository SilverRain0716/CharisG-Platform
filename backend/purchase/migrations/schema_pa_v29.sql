-- v29: coupons + coupon_items — 쿠팡 즉시할인쿠폰 발급/추적 테이블
--
-- 마진대 기반 5쿠폰 정책 발급 + 추적 (project_coupang_coupon_policy_pending.md):
--   1) PRICE 1,000원 / cap 10K  — margin 10K~15K
--   2) RATE  5%      / cap 10K  — margin 15K~70K
--   3) RATE  5%      / cap 20K  — margin 70K~100K
--   4) RATE  5%      / cap 30K  — margin 100K~150K
--   5) RATE  5%      / cap 50K  — margin 150K+
--
-- async 흐름: POST coupon → reqId → poll status DONE → couponId 회수 →
--             POST items(couponId, vendorItems) [10K청크] → reqId → poll DONE.

CREATE TABLE IF NOT EXISTS coupons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coupon_id INTEGER UNIQUE,                 -- Wing couponId (NULL until publish DONE)
    contract_id INTEGER NOT NULL,
    name TEXT NOT NULL,                        -- promotionName (≤45자)
    type TEXT NOT NULL,                        -- RATE / PRICE / FIXED_WITH_QUANTITY
    discount INTEGER NOT NULL,                 -- RATE: 1-100, PRICE: KRW
    max_discount_price INTEGER NOT NULL,
    wow_exclusive INTEGER NOT NULL DEFAULT 0,
    start_at TEXT NOT NULL,                    -- 'yyyy-MM-dd HH:mm:ss'
    end_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',  -- requested/active/expired/failed
    requested_id TEXT,                         -- 생성 reqId
    margin_band TEXT,                          -- '10K-15K' 등 우리 라벨
    target_count INTEGER NOT NULL DEFAULT 0,   -- 추가하려 한 vendorItem 수
    items_added INTEGER NOT NULL DEFAULT 0,    -- 실제 추가 성공
    items_failed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expired_at TEXT,
    last_synced_at TEXT,
    error_message TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_coupons_status ON coupons(status);
CREATE INDEX IF NOT EXISTS idx_coupons_coupon_id ON coupons(coupon_id);

CREATE TABLE IF NOT EXISTS coupon_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coupon_local_id INTEGER NOT NULL,          -- coupons.id (FK 우리쪽 PK)
    coupon_id INTEGER,                         -- Wing couponId (DONE 후 채워짐)
    vendor_item_id INTEGER NOT NULL,
    seller_product_id INTEGER,
    listing_id INTEGER,                        -- listings_pa.id
    status TEXT NOT NULL DEFAULT 'pending',    -- pending/applied/failed
    fail_reason TEXT,
    requested_id TEXT,                         -- 청크별 추가 reqId
    chunk_index INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(coupon_local_id, vendor_item_id)
);

CREATE INDEX IF NOT EXISTS idx_coupon_items_coupon ON coupon_items(coupon_local_id);
CREATE INDEX IF NOT EXISTS idx_coupon_items_vendor ON coupon_items(vendor_item_id);
CREATE INDEX IF NOT EXISTS idx_coupon_items_status ON coupon_items(status);
