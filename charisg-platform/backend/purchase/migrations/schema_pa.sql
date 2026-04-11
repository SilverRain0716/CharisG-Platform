-- Purchase Agent DB schema v1
-- 19 tables across 9 영역 (디스커버리/소싱/마진/통관/상품/주문/CS/반품/모니터링)
-- + translation_cache (backend_shared.ai)

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- [디스커버리] 1) keywords  2) keyword_clusters
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS keywords (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword         TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'naver_datalab',  -- naver_datalab|searchad|google_trends|manual
    cluster_id      INTEGER REFERENCES keyword_clusters(id),
    monthly_pc      INTEGER DEFAULT 0,
    monthly_mobile  INTEGER DEFAULT 0,
    competition     REAL,
    trend_score     REAL,
    status          TEXT DEFAULT 'new',                     -- new|reviewed|sourcing|skipped
    discovered_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(keyword, source)
);

CREATE TABLE IF NOT EXISTS keyword_clusters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL,
    representative  TEXT NOT NULL,                          -- 대표 키워드
    member_count    INTEGER DEFAULT 0,
    avg_volume      INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_keywords_status ON keywords(status);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- [소싱] 3) sourcing_candidates  4) margin_calcs  5) customs_checks
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS sourcing_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id          INTEGER REFERENCES keywords(id),
    asin                TEXT NOT NULL,
    title               TEXT,
    amazon_url          TEXT,
    image_url           TEXT,
    price_usd           REAL,
    rating              REAL,
    review_count        INTEGER DEFAULT 0,
    in_stock            INTEGER DEFAULT 1,
    cj_filter_pass      INTEGER DEFAULT 0,
    shipping_status     TEXT,                           -- PASS|WARN|REJECT
    shipping_reason     TEXT,
    sourcing_status     TEXT DEFAULT 'discovered',     -- discovered|reviewed|go|nogo
    nogo_reason         TEXT,
    collected_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(asin)
);

CREATE INDEX IF NOT EXISTS idx_src_status ON sourcing_candidates(sourcing_status);
CREATE INDEX IF NOT EXISTS idx_src_keyword ON sourcing_candidates(keyword_id);

CREATE TABLE IF NOT EXISTS margin_calcs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    sourcing_id             INTEGER REFERENCES sourcing_candidates(id),
    amazon_price_usd        REAL NOT NULL,
    fx_rate                 REAL NOT NULL,                  -- USD→KRW
    forwarder_fee_krw       REAL DEFAULT 0,
    return_reserve_krw      REAL DEFAULT 0,
    cs_cost_krw             REAL DEFAULT 0,
    sale_price_krw          REAL NOT NULL,                  -- 우리 판매가
    domestic_shipping_krw   REAL DEFAULT 0,
    customs_duty_krw        REAL DEFAULT 0,                 -- 예상 관부가세
    customer_total_krw      REAL,                           -- 고객 총 비용
    seller_net_krw          REAL,                           -- 우리 순익
    seller_margin_pct       REAL,
    competition             TEXT,                           -- HIGH|MED|LOW
    calculated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_margin_sourcing ON margin_calcs(sourcing_id);

CREATE TABLE IF NOT EXISTS customs_checks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sourcing_id         INTEGER REFERENCES sourcing_candidates(id),
    hs_code             TEXT,
    hs_source           TEXT DEFAULT 'ai',                  -- ai|manual|customs_db
    classification      TEXT,                               -- 목록통관|일반통관
    duty_rate           REAL,
    vat_rate            REAL DEFAULT 10,
    risk                TEXT,                               -- PASS|WARN|REJECT
    risk_reason         TEXT,
    checked_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- [상품] 6) products  7) listings_pa  8) detail_pages  9) upload_queue
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS products (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sourcing_id         INTEGER REFERENCES sourcing_candidates(id),
    business_model      TEXT NOT NULL DEFAULT 'purchase',
    asin                TEXT,
    title_en            TEXT,
    title_ko            TEXT,
    category_path       TEXT,                               -- 스마트스토어/쿠팡 카테고리
    sale_price_krw      REAL,
    cost_usd            REAL,
    margin_pct          REAL,
    bsr                 INTEGER,
    status              TEXT DEFAULT 'draft',               -- draft|ready|listed|active|paused|removed
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pa_products_status ON products(status);

CREATE TABLE IF NOT EXISTS listings_pa (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES products(id),
    channel             TEXT NOT NULL,                      -- smartstore|coupang
    channel_product_id  TEXT,
    status              TEXT DEFAULT 'pending',             -- pending|listed|active|paused|removed
    list_url            TEXT,
    last_synced_at      TEXT,
    error_message       TEXT,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, channel)
);

CREATE TABLE IF NOT EXISTS detail_pages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES products(id),
    template_id         INTEGER REFERENCES detail_templates(id),
    sections            TEXT NOT NULL,                      -- JSON 13 sections
    status              TEXT DEFAULT 'draft',               -- draft|reviewed|approved
    generated_by        TEXT DEFAULT 'gemini',
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS upload_queue (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES products(id),
    target_channels     TEXT NOT NULL,                      -- JSON ["smartstore", "coupang"]
    status              TEXT DEFAULT 'pending',             -- pending|uploading|success|failed
    result              TEXT,                               -- JSON {channel: {ok, message}}
    queued_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at          TEXT,
    finished_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_uq_status ON upload_queue(status);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- [템플릿] 10) detail_templates
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS detail_templates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    sections_template   TEXT NOT NULL,                      -- JSON
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- [주문·CS] 11) orders  12) order_steps  13) cs_tickets  14) returns_pa
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    business_model      TEXT NOT NULL DEFAULT 'purchase',
    channel             TEXT NOT NULL,                      -- smartstore|coupang
    channel_order_id    TEXT NOT NULL,
    product_id          INTEGER REFERENCES products(id),
    customer_name       TEXT,
    customer_phone      TEXT,
    address             TEXT,
    sale_price_krw      REAL,
    quantity            INTEGER NOT NULL DEFAULT 1,
    current_step        TEXT DEFAULT 'order_received',
    amazon_order_id     TEXT,
    forwarder_tracking  TEXT,
    domestic_tracking   TEXT,
    placed_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at        TEXT,
    UNIQUE(channel, channel_order_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_step ON orders(current_step);

CREATE TABLE IF NOT EXISTS order_steps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    step            TEXT NOT NULL,                          -- order_received|amazon_purchase|forwarder|international|domestic|completed
    label           TEXT NOT NULL,
    started_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at     TEXT,
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_steps_order ON order_steps(order_id);

CREATE TABLE IF NOT EXISTS cs_tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel         TEXT NOT NULL,
    order_id        INTEGER REFERENCES orders(id),
    customer_name   TEXT,
    type            TEXT,                                   -- 배송지연|품질문제|환불요청|기타
    priority        TEXT DEFAULT 'normal',                  -- urgent|high|normal|low
    status          TEXT DEFAULT 'open',                    -- open|in_progress|resolved|closed
    customer_message TEXT,
    ai_draft        TEXT,
    final_response  TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_cs_status ON cs_tickets(status);

CREATE TABLE IF NOT EXISTS returns_pa (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id),
    reason          TEXT,
    status          TEXT DEFAULT 'requested',               -- requested|approved|received|refunded|rejected
    refund_krw      REAL,
    requested_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    refunded_at     TEXT
);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- [모니터링] 15) price_history  16) stock_alerts  17) competition_snapshots
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS price_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    amazon_price_usd REAL,
    fx_rate         REAL,
    margin_pct      REAL,
    captured_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ph_product ON price_history(product_id);

CREATE TABLE IF NOT EXISTS stock_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    type            TEXT,                                   -- out_of_stock|low_stock|delayed
    detected_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS competition_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES products(id),
    competitor_channel  TEXT NOT NULL,                      -- coupang|smartstore|11st|gmarket
    competitor_price    REAL,
    rank                INTEGER,
    captured_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- [참조 데이터] 18) tariff_codes  19) settings
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS tariff_codes (
    hs_code         TEXT PRIMARY KEY,
    description_ko  TEXT,
    description_en  TEXT,
    duty_rate       REAL,
    vat_rate        REAL DEFAULT 10,
    simple_clearance INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO settings(key, value) VALUES
    ('margin.forwarder_fee_krw', '5000'),
    ('margin.return_reserve_pct', '3'),
    ('margin.cs_cost_krw', '2000'),
    ('margin.fx_rate_source', 'manual'),
    ('margin.default_fx_rate', '1380'),
    ('crawl.naver_datalab_cron', '0 4 * * *'),
    ('crawl.amazon_cron', '0 5 * * *'),
    ('discord_webhook', '');

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 번역 캐시
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS translation_cache (
    source_text_hash    TEXT NOT NULL,
    source_lang         TEXT NOT NULL,
    target_lang         TEXT NOT NULL,
    translated_text     TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_text_hash, source_lang, target_lang)
);
