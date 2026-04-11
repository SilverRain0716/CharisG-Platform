-- Dropshipping DB schema v1
-- 6 tables: collected_products, amazon_search_results, amazon_search_agg,
--           account_health, listings, sales

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 1) collected_products — CJ 카탈로그 + 스코어링 결과
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS collected_products (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT NOT NULL DEFAULT 'cj',
    business_model      TEXT NOT NULL DEFAULT 'dropship',  -- 항상 'dropship'
    external_id         TEXT NOT NULL,                      -- CJ pid
    url                 TEXT,
    product_name        TEXT,
    category            TEXT,                               -- CJ 원본 카테고리
    amazon_category     TEXT,                               -- 매핑된 Amazon 카테고리
    image_url           TEXT,
    image_count         INTEGER DEFAULT 0,

    -- 가격
    source_price        REAL,                               -- CJ 매입가 (USD)
    source_currency     TEXT DEFAULT 'USD',
    shipping_cost       REAL DEFAULT 0,
    calculated_price    REAL,                               -- 우리 판매가 (USD)
    margin_pct          REAL,                               -- 표면 마진율
    real_margin_pct     REAL,                               -- Referral Fee 반영 마진율
    adjusted_margin_pct REAL,                               -- Amazon p75 기준 마진율

    -- CJ 메타
    us_warehouse        INTEGER DEFAULT 0,
    stock_quantity      INTEGER DEFAULT 0,
    weight_g            INTEGER DEFAULT 0,

    -- Hard Filter
    hard_filter_pass    INTEGER DEFAULT 0,
    filter_fail_reason  TEXT,                               -- no_us_warehouse|low_margin|low_stock|price_range|overweight|few_images|blocked_brand|blocked_category

    -- 스코어링
    trend_score         REAL,
    demand_score        REAL,
    demand_grade        TEXT,                               -- A|B|C
    gap_score           REAL DEFAULT 1.0,
    margin_score        REAL,
    margin_grade        TEXT,                               -- A|B|C
    matrix_group        TEXT,                               -- AA|AB|...|CC
    sort_score          REAL,                               -- D × G × M
    score               REAL,                               -- 0-100 호환 (sort_score × 100)
    grade               TEXT,                               -- matrix_group 호환

    -- 키워드 + GO 판정
    search_keyword      TEXT,
    go_decision         TEXT,                               -- GO|GO_ORGANIC|SKIP
    price_position      TEXT,                               -- competitive|premium|exceeded
    amazon_price_p75    REAL,                               -- 키워드 p75 캐시 (조인 회피용)

    -- 상태
    status              TEXT DEFAULT 'raw',                 -- raw|filtered|candidate|listed|active|paused
    processing_status   TEXT DEFAULT 'raw',
    tier                TEXT,                               -- tier1|tier2

    collected_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_cp_status ON collected_products(status);
CREATE INDEX IF NOT EXISTS idx_cp_go ON collected_products(go_decision);
CREATE INDEX IF NOT EXISTS idx_cp_sort ON collected_products(sort_score DESC);
CREATE INDEX IF NOT EXISTS idx_cp_matrix ON collected_products(matrix_group);
CREATE INDEX IF NOT EXISTS idx_cp_keyword ON collected_products(search_keyword);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 2) amazon_search_results — 키워드별 개별 리스팅 raw
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS amazon_search_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword         TEXT NOT NULL,
    asin            TEXT NOT NULL,
    title           TEXT,
    price           REAL,
    review_count    INTEGER DEFAULT 0,
    is_fbm          INTEGER DEFAULT 0,
    bsr             INTEGER,
    image_url       TEXT,
    collected_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(keyword, asin)
);

CREATE INDEX IF NOT EXISTS idx_asr_keyword ON amazon_search_results(keyword);
CREATE INDEX IF NOT EXISTS idx_asr_collected ON amazon_search_results(collected_at);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 3) amazon_search_agg — 키워드별 가격/리뷰 집계 (Gap Score 입력)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS amazon_search_agg (
    keyword             TEXT PRIMARY KEY,
    price_min           REAL,
    price_p25           REAL,
    price_median        REAL,
    price_p75           REAL,
    price_max           REAL,
    avg_review_count    INTEGER DEFAULT 0,
    min_review_count    INTEGER DEFAULT 0,
    fbm_count           INTEGER DEFAULT 0,
    total_results       INTEGER DEFAULT 0,
    collected_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 4) account_health — Amazon 셀러 계정 건강도 (Phase 0: 수동 입력)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS account_health (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    odr                     REAL,            -- Order Defect Rate (%) — 목표 < 0.5
    late_shipment_rate      REAL,            -- LSR (%) — 목표 < 2
    cancel_rate             REAL,            -- (%) — 목표 < 1
    valid_tracking_rate     REAL,            -- VTR (%) — 목표 > 99
    input_type              TEXT DEFAULT 'manual',  -- manual|sp_api
    note                    TEXT,
    updated_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 5) listings — 리스팅 라이프사이클 추적
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS listings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES collected_products(id),
    business_model      TEXT NOT NULL DEFAULT 'dropship',
    asin                TEXT,
    sku                 TEXT,
    listing_url         TEXT,
    tier                TEXT,                            -- tier1|tier2
    status              TEXT DEFAULT 'candidate',        -- candidate|listed|active|paused|removed
    title               TEXT,
    bullets             TEXT,                            -- JSON array
    description         TEXT,
    keywords            TEXT,                            -- JSON array
    listed_at           TEXT,
    activated_at        TEXT,
    paused_at           TEXT,
    pause_reason        TEXT,
    last_price          REAL,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_product ON listings(product_id);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 6) sales — 판매 데이터 (Phase 1+ SP-API 연동)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS sales (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id          INTEGER REFERENCES listings(id),
    product_id          INTEGER REFERENCES collected_products(id),
    order_id            TEXT,
    asin                TEXT,
    sku                 TEXT,
    quantity            INTEGER NOT NULL DEFAULT 1,
    unit_price          REAL,
    total_revenue       REAL,
    cogs                REAL,
    shipping_cost       REAL,
    referral_fee        REAL,
    ad_spend            REAL,
    net_profit          REAL,
    margin_pct          REAL,
    purchased_at        TEXT,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sales_listing ON sales(listing_id);
CREATE INDEX IF NOT EXISTS idx_sales_purchased ON sales(purchased_at);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 번역 캐시 (backend_shared.ai 가 사용)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS translation_cache (
    source_text_hash    TEXT NOT NULL,
    source_lang         TEXT NOT NULL,
    target_lang         TEXT NOT NULL,
    translated_text     TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_text_hash, source_lang, target_lang)
);
