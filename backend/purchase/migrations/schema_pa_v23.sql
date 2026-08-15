-- v23: 카테고리 매핑 캐시 + 검토 큐 (Fix 1-D)
--
-- 배경: find_category_with_gemini 가 score 0~100 반환 — 임계값(50) 미만은 자동 적용
-- 안 하고 사용자 검토. 검토 끝난 매핑은 keyword_category_map 에 캐시 → 같은 키워드 재매핑
-- 시 AI 호출 없이 즉시 반환.
--
-- 두 테이블:
--   keyword_category_map  — 시트의 "카테고리(키워드)" 컬럼 → 네이버/쿠팡 카테고리 영구 매핑
--   category_review_queue — AI score < 50 또는 사용자 강제 review 요청 시 들어오는 큐

CREATE TABLE IF NOT EXISTS keyword_category_map (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword                     TEXT NOT NULL UNIQUE,        -- 정규화된 lowercase 키워드
    naver_category_id           TEXT,                         -- 네이버 wholeCategoryId
    naver_category_path         TEXT,                         -- "디지털/가전 > ... > 액정보호필름"
    coupang_category_code       INTEGER,                      -- 쿠팡 displayCategoryCode
    coupang_category_path       TEXT,                         -- "가전/디지털 > ... > 전면보호"
    source                      TEXT NOT NULL DEFAULT 'manual',  -- 'manual'|'ai'|'verified'
    ai_naver_score              INTEGER,                      -- 0~100
    ai_coupang_score            INTEGER,
    notes                       TEXT,
    created_at                  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_keyword_category_map_keyword
    ON keyword_category_map(keyword);
CREATE INDEX IF NOT EXISTS idx_keyword_category_map_source
    ON keyword_category_map(source);


CREATE TABLE IF NOT EXISTS category_review_queue (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id                  INTEGER REFERENCES products(id),
    keyword                     TEXT,                         -- 시트에서 온 키워드 (있으면)
    product_name                TEXT NOT NULL,                -- 검토 시 보여줄 이름 (한국어)
    product_name_en             TEXT,                         -- 원문 영문 (참조용)

    -- AI 추천
    ai_naver_id                 TEXT,
    ai_naver_path               TEXT,
    ai_naver_score              INTEGER,
    ai_naver_reason             TEXT,
    ai_coupang_code             INTEGER,
    ai_coupang_path             TEXT,
    ai_coupang_score            INTEGER,
    ai_coupang_reason           TEXT,

    -- 사용자 확정
    status                      TEXT NOT NULL DEFAULT 'pending',  -- 'pending'|'approved'|'rejected'
    approved_naver_id           TEXT,
    approved_naver_path         TEXT,
    approved_coupang_code       INTEGER,
    approved_coupang_path       TEXT,

    reviewer                    TEXT,
    reviewed_at                 TEXT,
    notes                       TEXT,
    created_at                  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_category_review_status
    ON category_review_queue(status);
CREATE INDEX IF NOT EXISTS idx_category_review_product
    ON category_review_queue(product_id);
CREATE INDEX IF NOT EXISTS idx_category_review_keyword
    ON category_review_queue(keyword) WHERE keyword IS NOT NULL;
