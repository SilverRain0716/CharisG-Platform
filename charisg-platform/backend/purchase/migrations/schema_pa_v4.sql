-- Purchase Agent DB schema v4
-- 상품관리 확장 + 채널별 listings + 이미지 캐시 + settings 시드
-- 2026-04-11
--
-- 변경 내용:
--   1) products: AI 가공 산출물 (번역/이미지/스펙/브랜드/무게) + 가공 시각
--   2) listings_pa: 채널별 가격/수수료/마진/카테고리/출고지 + 메타 JSON
--   3) image_cache: EC2 로컬 이미지 저장 + 업로드 후 자동 삭제 스케줄
--   4) settings: 환율/마진/수수료/배송비/이미지 보관 기본값 시드

-- products 확장
ALTER TABLE products ADD COLUMN description_ko TEXT;
ALTER TABLE products ADD COLUMN description_en TEXT;
ALTER TABLE products ADD COLUMN images_json TEXT;
ALTER TABLE products ADD COLUMN specs_json TEXT;
ALTER TABLE products ADD COLUMN brand TEXT;
ALTER TABLE products ADD COLUMN weight_g INTEGER;
ALTER TABLE products ADD COLUMN ai_processed_at TEXT;

-- listings_pa 확장 (채널별 가격/카테고리/출고지/메타)
ALTER TABLE listings_pa ADD COLUMN sale_krw INTEGER;
ALTER TABLE listings_pa ADD COLUMN cost_krw_snapshot INTEGER;
ALTER TABLE listings_pa ADD COLUMN fee_rate REAL;
ALTER TABLE listings_pa ADD COLUMN net_margin_krw INTEGER;
ALTER TABLE listings_pa ADD COLUMN category_mapped TEXT;
ALTER TABLE listings_pa ADD COLUMN shipping_origin TEXT;
ALTER TABLE listings_pa ADD COLUMN meta_json TEXT;

-- image_cache: EC2 로컬 이미지 저장소 + 업로드 후 삭제 스케줄
CREATE TABLE IF NOT EXISTS image_cache (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id            INTEGER NOT NULL REFERENCES products(id),
    local_path            TEXT NOT NULL,
    public_url            TEXT NOT NULL,
    original_url          TEXT,
    image_idx             INTEGER NOT NULL DEFAULT 0,
    size_bytes            INTEGER,
    downloaded_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    uploaded_to_channels  TEXT,
    scheduled_delete_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_image_cache_product ON image_cache(product_id);
CREATE INDEX IF NOT EXISTS idx_image_cache_delete  ON image_cache(scheduled_delete_at);

-- settings 시드: 환율/마진/수수료/배송/이미지 보관 (INSERT OR IGNORE 로 멱등)
INSERT OR IGNORE INTO settings (key, value) VALUES
  ('exchange_rate_usd_krw',          '1430'),
  ('exchange_rate_updated_at',       ''),
  ('margin_target_rate',             '0.35'),
  ('smartstore_fee_rate',            '0.0548'),
  ('coupang_fee_rate',               '0.1374'),
  ('amazon_shipping_default_usd',    '0'),
  ('cj_shipping_default_usd_per_kg', '12'),
  ('image_retention_days',           '30');
