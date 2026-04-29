-- purchase_hot.db — 실시간 운영 테이블 분리 (orders + 관련)
--
-- 배경: 대량 batch 처리 (detailing/upload) 가 SQLite 락을 점유해서 orders/cs/returns
-- 페이지가 timeout 되는 lock contention 문제. orders 그룹을 별도 DB 로 분리하여 격리.
--
-- 분리 원칙:
--   hot.db: 실시간 운영 (orders, order_steps, cs_tickets, returns_pa)
--   cold.db (기존 purchase.db): 대량 처리 (products, listings_pa, batch_jobs 등)
--
-- Cross-DB 접근:
--   - Hot path (orders 페이지): denormalized 컬럼으로 cold.db 안 봄
--   - Cold path (dashboard 통계): ATTACH DATABASE 로 cross-DB JOIN

CREATE TABLE IF NOT EXISTS orders (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    business_model          TEXT NOT NULL DEFAULT 'purchase',
    channel                 TEXT NOT NULL,
    channel_order_id        TEXT NOT NULL,
    product_id              INTEGER,                              -- cold.db 의 products.id (FK 못 걸음)
    customer_name           TEXT,
    customer_phone          TEXT,
    address                 TEXT,
    sale_price_krw          REAL,
    quantity                INTEGER NOT NULL DEFAULT 1,
    current_step            TEXT DEFAULT 'order_received',
    amazon_order_id         TEXT,
    forwarder_tracking      TEXT,
    domestic_tracking       TEXT,
    placed_at               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at            TEXT,
    customs_clearance_code  TEXT,
    orderer_real_phone      TEXT,
    shipping_message        TEXT,
    external_sku            TEXT,
    ordered_at              TEXT,
    paid_at                 TEXT,
    customer_name_en        TEXT,
    address_en              TEXT,
    translation_status      TEXT DEFAULT 'pending',
    shipping_method         TEXT,
    child_product_id        INTEGER,                              -- cold.db 의 products.id (FK 못 걸음)
    child_asin              TEXT,

    -- Denormalized columns (cold.db 안 봐도 표시 가능, INSERT 시 채움)
    product_name_cache      TEXT,                                 -- products.title_ko snapshot
    product_image_cache     TEXT,                                 -- products.images_json 의 첫 번째 URL
    brand_cache             TEXT,                                 -- products.brand
    asin_cache              TEXT,                                 -- products.asin

    UNIQUE(channel, channel_order_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_step ON orders(current_step);
CREATE INDEX IF NOT EXISTS idx_orders_placed_at ON orders(placed_at);
CREATE INDEX IF NOT EXISTS idx_orders_product_id ON orders(product_id);


CREATE TABLE IF NOT EXISTS order_steps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    step            TEXT NOT NULL,
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
    type            TEXT,
    priority        TEXT DEFAULT 'normal',
    status          TEXT DEFAULT 'open',
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
    status          TEXT DEFAULT 'requested',
    refund_krw      REAL,
    requested_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    refunded_at     TEXT
);
