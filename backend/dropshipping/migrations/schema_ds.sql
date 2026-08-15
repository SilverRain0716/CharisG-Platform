-- Dropshipping DB schema v1 (monolith control_tower.db 호환 superset)
-- 6 핵심 테이블 + translation_cache + auxiliary
-- collected_products / amazon_search_results / amazon_search_agg /
-- account_health / listings / sales

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 1) collected_products — CJ 카탈로그 + 스코어링 결과
--    monolith 69 cols 호환 (job_id, brand, description, etc.)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS collected_products (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id                  INTEGER,
    import_job_id           INTEGER,
    source                  TEXT NOT NULL DEFAULT 'cj',
    business_model          TEXT NOT NULL DEFAULT 'dropship',
    external_id             TEXT,
    url                     TEXT,
    product_name            TEXT NOT NULL,
    product_name_kr         TEXT,
    product_name_processed  TEXT,
    category                TEXT,
    category_mapped         TEXT,
    amazon_category         TEXT,
    brand                   TEXT,
    description             TEXT,
    description_kr          TEXT,
    specs                   TEXT DEFAULT '{}',
    options                 TEXT DEFAULT '[]',
    image_url               TEXT,
    images                  TEXT DEFAULT '[]',
    images_processed        TEXT DEFAULT '[]',
    image_count             INTEGER DEFAULT 0,

    -- 가격
    source_price            REAL,
    source_currency         TEXT DEFAULT 'USD',
    calculated_price        REAL,
    margin_pct              REAL,
    real_margin_pct         REAL,
    adjusted_margin_pct     REAL,
    shipping_cost           REAL DEFAULT 0,

    -- 메타 / 재고
    stock_status            TEXT,
    stock_quantity          INTEGER DEFAULT 0,
    shipping_type           TEXT,
    delivery_days           INTEGER,
    warehouse               TEXT,                              -- monolith: 'USA-CA' 등
    us_warehouse            INTEGER DEFAULT 0,                 -- 우리 추가: 파생값 (warehouse LIKE 'US%')
    weight_g                REAL,

    -- 외부 지표
    review_count            INTEGER,
    rating                  REAL,
    rank                    INTEGER,
    rank_change             TEXT,

    -- Hard Filter
    hard_filter_pass        BOOLEAN DEFAULT 0,
    filter_fail_reason      TEXT,

    -- Scoring (3축)
    trend_score             REAL,
    demand_score            REAL,
    demand_grade            TEXT,
    gap_score               REAL DEFAULT 1.0,
    gap_grade               TEXT,
    margin_score            REAL,
    margin_grade            TEXT,
    matrix_group            TEXT,
    sort_score              REAL,
    score                   REAL,
    grade                   TEXT,
    final_score             REAL,
    final_grade             TEXT,

    -- 키워드 + GO 판정
    search_keyword          TEXT,
    go_decision             TEXT,
    price_position          TEXT,
    amazon_price_median     REAL,
    amazon_price_p75        REAL,

    -- 처리 상태 (모노리스 호환)
    processing_status       TEXT DEFAULT 'raw',
    translation_done        BOOLEAN DEFAULT 0,
    images_done             BOOLEAN DEFAULT 0,
    detail_page_done        BOOLEAN DEFAULT 0,
    listing_status          TEXT DEFAULT 'collected',
    listed_shop_id          INTEGER,
    listed_product_id       TEXT,
    listed_at               DATETIME,
    status                  TEXT DEFAULT 'candidate',          -- candidate|listed|active|paused|removed|filtered
    tier                    TEXT,

    collected_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_cp_status      ON collected_products(status);
CREATE INDEX IF NOT EXISTS idx_cp_go          ON collected_products(go_decision);
CREATE INDEX IF NOT EXISTS idx_cp_sort        ON collected_products(sort_score DESC);
CREATE INDEX IF NOT EXISTS idx_cp_matrix      ON collected_products(matrix_group);
CREATE INDEX IF NOT EXISTS idx_cp_keyword     ON collected_products(search_keyword);
CREATE INDEX IF NOT EXISTS idx_cp_business    ON collected_products(business_model);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 2) amazon_search_results — 키워드별 개별 리스팅 raw (monolith 12 cols 호환)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS amazon_search_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword         TEXT NOT NULL,
    asin            TEXT NOT NULL,
    title           TEXT,
    price           REAL,
    review_count    INTEGER DEFAULT 0,
    rating          REAL,
    is_prime        INTEGER DEFAULT 0,
    is_fba          INTEGER DEFAULT 0,
    is_fbm          INTEGER DEFAULT 0,
    seller_type     TEXT,
    position        INTEGER,
    bsr             INTEGER,
    image_url       TEXT,
    collected_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(keyword, asin)
);

CREATE INDEX IF NOT EXISTS idx_asr_keyword   ON amazon_search_results(keyword);
CREATE INDEX IF NOT EXISTS idx_asr_collected ON amazon_search_results(collected_at);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 3) amazon_search_agg — 키워드별 집계 (monolith 15 cols 호환, Gap Score 입력)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS amazon_search_agg (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword             TEXT UNIQUE NOT NULL,
    price_min           REAL,
    price_p25           REAL,
    price_median        REAL,
    price_p75           REAL,
    price_max           REAL,
    avg_review_count    REAL DEFAULT 0,
    min_review_count    INTEGER DEFAULT 0,
    avg_rating          REAL,
    prime_count         INTEGER DEFAULT 0,
    fba_count           INTEGER DEFAULT 0,
    fbm_count           INTEGER DEFAULT 0,
    total_results       INTEGER DEFAULT 0,
    collected_at        DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 4) account_health — Amazon 셀러 계정 건강도 (Phase 0: 수동 입력)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS account_health (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    odr                     REAL,            -- < 0.5 목표
    late_shipment_rate      REAL,            -- < 2 목표
    cancel_rate             REAL,            -- < 1 목표
    valid_tracking_rate     REAL,            -- > 99 목표
    input_type              TEXT DEFAULT 'manual',  -- manual|sp_api
    note                    TEXT,
    updated_at              DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 5) listings — 리스팅 (monolith 11 cols + 우리 추가 필드 superset)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS listings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER,                          -- collected_products(id)
    shop_id             INTEGER,                          -- monolith: shops 테이블 ref
    platform            TEXT,                             -- amazon|smartstore|coupang
    market_product_id   TEXT,                             -- monolith 호환
    business_model      TEXT NOT NULL DEFAULT 'dropship',
    asin                TEXT,
    sku                 TEXT,
    listing_url         TEXT,
    tier                TEXT,                             -- tier1|tier2
    status              TEXT DEFAULT 'candidate',
    title               TEXT,
    bullets             TEXT,                             -- JSON
    description         TEXT,
    keywords            TEXT,                             -- JSON
    current_price       REAL,
    current_stock       INTEGER,
    last_price          REAL,
    listed_at           DATETIME,
    activated_at        DATETIME,
    paused_at           DATETIME,
    pause_reason        TEXT,
    last_synced_at      DATETIME,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_listings_status   ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_product  ON listings(product_id);
CREATE INDEX IF NOT EXISTS idx_listings_business ON listings(business_model);

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
    purchased_at        DATETIME,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sales_listing   ON sales(listing_id);
CREATE INDEX IF NOT EXISTS idx_sales_purchased ON sales(purchased_at);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 7) asin_match_candidates — ASIN 매칭 후보 (검색 결과 + 점수)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS asin_match_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL,
    asin            TEXT NOT NULL,
    amazon_title    TEXT,
    amazon_brand    TEXT,
    amazon_price    REAL,
    title_sim       REAL,
    price_compat    REAL,
    match_score     REAL,
    match_verdict   TEXT,       -- strong|moderate|weak|reject
    selected        BOOLEAN DEFAULT 0,
    searched_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, asin)
);

CREATE INDEX IF NOT EXISTS idx_amc_product ON asin_match_candidates(product_id);
CREATE INDEX IF NOT EXISTS idx_amc_verdict ON asin_match_candidates(match_verdict);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 번역 캐시 (backend_shared.ai 가 사용)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS translation_cache (
    source_text_hash    TEXT NOT NULL,
    source_lang         TEXT NOT NULL,
    target_lang         TEXT NOT NULL,
    translated_text     TEXT NOT NULL,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_text_hash, source_lang, target_lang)
);
