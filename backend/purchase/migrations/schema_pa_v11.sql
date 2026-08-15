-- v11: 네이버 ↔ 쿠팡 카테고리 매핑 테이블
CREATE TABLE IF NOT EXISTS naver_coupang_category_map (
    naver_id      TEXT PRIMARY KEY,
    coupang_code  INTEGER NOT NULL,
    method        TEXT NOT NULL,             -- 'exact' | 'path' | 'ai'
    confidence    REAL NOT NULL DEFAULT 1.0, -- 0.0 ~ 1.0
    note          TEXT,
    mapped_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_naver_coupang_map_method ON naver_coupang_category_map(method);
CREATE INDEX IF NOT EXISTS idx_naver_coupang_map_coupang ON naver_coupang_category_map(coupang_code);
