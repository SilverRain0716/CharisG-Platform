-- v10: 쿠팡 카테고리 트리 (16,392 leaf, 19,461 nodes)
-- 매 등록 시 displayCategoryCode 검증 + 이름 기반 자동 매칭에 활용.

CREATE TABLE IF NOT EXISTS coupang_categories (
    code         INTEGER PRIMARY KEY,           -- displayItemCategoryCode
    name         TEXT NOT NULL,                 -- 마지막 leaf 이름
    path         TEXT NOT NULL,                 -- "대분류 > 중분류 > ... > leaf"
    depth        INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ACTIVE', -- ACTIVE / INACTIVE
    is_leaf      INTEGER NOT NULL DEFAULT 1,
    parent_code  INTEGER,
    synced_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_coupang_categories_name ON coupang_categories(name);
CREATE INDEX IF NOT EXISTS idx_coupang_categories_status ON coupang_categories(status);
CREATE INDEX IF NOT EXISTS idx_coupang_categories_parent ON coupang_categories(parent_code);

-- listings_pa.category_mapped 재매핑 추적 컬럼
ALTER TABLE listings_pa ADD COLUMN coupang_category_code INTEGER;
ALTER TABLE listings_pa ADD COLUMN coupang_category_resolved_at TEXT;
