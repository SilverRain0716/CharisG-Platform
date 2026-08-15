-- v25: 키워드 N:M 매핑 (product_keywords)
--
-- 배경: 같은 ASIN 이 여러 시트/키워드에서 발견될 수 있는데 products.sourcing_id 가 1:1 라
-- 첫 키워드만 추적 가능. promote 시점 IntegrityError → 전체 fail 도 같은 뿌리.
--
-- 해결: product_keywords 로 N:M 매핑. promote 시 신규 ASIN 은 자동 INSERT (is_primary=1).
-- 중복 ASIN 의 새 키워드는 사용자 UI 액션으로만 추가 (is_primary=0).
--
-- 백필 안 함 — sourcing_candidates 가 promote 후 DELETE 되어 기존 products 의 키워드 추적 불가.
-- 신규 promote 부터 정상 동작.

CREATE TABLE IF NOT EXISTS product_keywords (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    keyword             TEXT NOT NULL,
    source_sourcing_id  INTEGER,                          -- 어느 시트/세션에서 왔는지
    is_primary          INTEGER NOT NULL DEFAULT 0,       -- 첫 키워드 (promote 시점 자동 등록)
    added_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, keyword)
);

CREATE INDEX IF NOT EXISTS idx_pk_product ON product_keywords(product_id);
CREATE INDEX IF NOT EXISTS idx_pk_keyword ON product_keywords(keyword);
CREATE INDEX IF NOT EXISTS idx_pk_primary ON product_keywords(is_primary);
