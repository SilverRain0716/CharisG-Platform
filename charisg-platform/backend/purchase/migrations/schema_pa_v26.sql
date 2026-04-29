-- v26: listings_pa.coupang_auto_matched (B 안 차등 처리)
--
-- 배경: 카테고리 매핑 score 50~70 구간 (모호하지만 거의 적합) 은 쿠팡 자동매칭으로 위임.
-- 매핑 결과를 keyword_category_map 에 저장하지 않고 listings_pa.coupang_auto_matched=1
-- 으로 마킹 → 쿠팡 등록 시 category=0 으로 보내서 쿠팡 AI 가 카테고리 결정.
-- score < 50 은 review_queue 강제 (사용자 검토 필수).

ALTER TABLE listings_pa ADD COLUMN coupang_auto_matched INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_listings_pa_auto_matched
    ON listings_pa(coupang_auto_matched) WHERE coupang_auto_matched=1;
