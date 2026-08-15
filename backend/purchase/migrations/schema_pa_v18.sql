-- v18: Phase 3 Variation 통합 등록 인프라
--
-- 배경: SP-API 백필(v17)로 6,098 products 가 parent_asin 보유 → 같은 모델의 size/flavor/color
-- 변형 옵션을 별도 ASIN 으로 가지고 있다는 의미. 채널에 multi-option 한 상품으로 등록할 수
-- 있다면 노출/구매 효율 대폭 향상. 단 STANLEY 같이 옵션 200+ 케이스는 채널 한도(쿠팡 30 /
-- 네이버 100) 초과 → 자동 분리 필요.
--
-- 추가 요소:
--   1. products 의 group 관련 메타 컬럼
--   2. listings_pa UNIQUE 완화 (한 product 가 여러 listing — 사이즈별 분리 가능)
--   3. listing_options 신설 — 한 listing 안의 옵션 정보 (channel_option_id 별)
--   4. orders 에 child_product_id / child_asin — 어느 옵션이 팔렸는지
--   5. category_split_rules — 분리 차원 우선순위 학습 누적

ALTER TABLE products ADD COLUMN group_master_asin TEXT;
ALTER TABLE products ADD COLUMN is_group_master INTEGER DEFAULT 0;
ALTER TABLE products ADD COLUMN option_label TEXT;
ALTER TABLE products ADD COLUMN option_dimensions_json TEXT;

CREATE INDEX IF NOT EXISTS idx_products_group_master_asin
  ON products(group_master_asin) WHERE group_master_asin IS NOT NULL;

-- listings_pa 의 기존 UNIQUE(product_id, channel) 제약은 사이즈별 분리 등록을 막으므로
-- (channel, channel_product_id) UNIQUE 로 전환. 단, channel_product_id NULL 인 pending
-- 행은 중복 가능하므로 부분 인덱스로.
-- 기존 인덱스 이름이 sqlite 자동생성 (sqlite_autoindex_listings_pa_1) 이라 직접 DROP 못함.
-- 대안: 신규 인덱스만 추가하고, 기존 product_id+channel UNIQUE 는 살려둠 (대부분 케이스
-- 단일 listing 이라 충돌 없음). 사이즈별 분리는 product_id 가 size 별 master 로 다르게
-- 지정되므로 자연 unique.
CREATE UNIQUE INDEX IF NOT EXISTS uq_listings_pa_channel_cpid
  ON listings_pa(channel, channel_product_id) WHERE channel_product_id IS NOT NULL AND channel_product_id != '';

CREATE TABLE IF NOT EXISTS listing_options (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id          INTEGER NOT NULL REFERENCES listings_pa(id) ON DELETE CASCADE,
  child_product_id    INTEGER NOT NULL REFERENCES products(id),
  option_label        TEXT NOT NULL,
  channel_option_id   TEXT,                          -- 쿠팡 vendorItemId / 네이버 channelProductNo
  sale_krw            INTEGER,
  cost_krw_snapshot   INTEGER,
  net_margin_krw      INTEGER,
  stock               INTEGER DEFAULT 100,
  status              TEXT DEFAULT 'active',         -- active|paused|removed
  option_image_url    TEXT,
  last_synced_at      TEXT,
  created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(listing_id, child_product_id)
);

CREATE INDEX IF NOT EXISTS idx_listing_options_listing
  ON listing_options(listing_id);
CREATE INDEX IF NOT EXISTS idx_listing_options_channel_option_id
  ON listing_options(channel_option_id) WHERE channel_option_id IS NOT NULL;

ALTER TABLE orders ADD COLUMN child_product_id INTEGER REFERENCES products(id);
ALTER TABLE orders ADD COLUMN child_asin TEXT;

-- 분리 차원 우선순위 학습 (사용자 UI 오버라이드 누적)
-- 초기엔 빈 테이블, 사용자가 GroupDetailPage 에서 차원 변경 시 INSERT/UPDATE
CREATE TABLE IF NOT EXISTS category_split_rules (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  category_path            TEXT NOT NULL UNIQUE,
  preferred_dim_priority   TEXT NOT NULL,             -- JSON ["color","style","size"]
  source                   TEXT NOT NULL,             -- 'auto'|'user'|'ai'
  sample_count             INTEGER DEFAULT 0,
  notes                    TEXT,
  updated_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

-- group discovery 캐시: 같은 parent_asin 의 childAsins 수만 빠르게 알아두기.
-- 실제 children facts 는 사용자 등록 시점에 fetch_full_catalog_facts 로 lazy load.
CREATE TABLE IF NOT EXISTS variation_groups (
  parent_asin              TEXT PRIMARY KEY,
  variation_theme          TEXT,                      -- "SIZE/COLOR" 등
  variation_dimensions     TEXT,                      -- JSON ["size","color"]
  child_asins_json         TEXT,                      -- 전체 childAsins 리스트
  child_count              INTEGER DEFAULT 0,
  category_path            TEXT,                      -- master 의 category_path 캐시
  master_asin              TEXT,                      -- 자동 결정된 master ASIN
  brand                    TEXT,
  base_name_en             TEXT,                      -- 옵션 차원 빼고 base 상품명
  base_name_ko             TEXT,
  ingestion_status         TEXT DEFAULT 'discovered', -- 'discovered'|'children_loaded'|'listed'
  discovered_at            TEXT NOT NULL DEFAULT (datetime('now')),
  children_loaded_at       TEXT,
  notes                    TEXT
);

CREATE INDEX IF NOT EXISTS idx_variation_groups_status
  ON variation_groups(ingestion_status);
CREATE INDEX IF NOT EXISTS idx_variation_groups_child_count
  ON variation_groups(child_count);
