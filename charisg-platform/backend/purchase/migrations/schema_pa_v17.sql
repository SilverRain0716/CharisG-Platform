-- v17: products 테이블에 SP-API Catalog facts 캐시 컬럼 추가
--
-- 배경: 소싱(sourcing_promote) / 이미지(image_downloader) / 사후 보정(coupang_attributes)
-- 세 곳에서 SP-API CatalogItems 를 호출하지만 각자 다른 includedData 만 가져오고,
-- 풍부한 데이터(dimensions, relationships, attributes 의 net_content_volume, item_weight,
-- total_servings, parent_asin 등)를 받아도 저장하지 않거나 일부만 저장한다.
--
-- 결과적으로:
--  1. 같은 ASIN 에 SP-API 호출이 중복 발생 (소싱 + 사후 strict)
--  2. 채널 등록 시 무게/사이즈/parent ASIN 등 정확한 값을 다시 추출 시도 (실패율 높음)
--  3. variation 통합 등록(같은 모델의 사이즈/맛 옵션 합치기)이 불가능
--
-- 해결: 단일 facts 모듈(sp_api_facts.py)이 SP-API 한 번 호출로 모든 데이터를 받아
-- 정규화 후 sp_api_facts_json 에 통째로 저장. parent_asin 만 별도 컬럼으로 분리해
-- variation 통합 등록 시 빠른 조회/조인 가능.
--
-- 기존 products 행은 sp_api_facts_at IS NULL 상태 → 백필 스크립트가 점진적으로 채움.

ALTER TABLE products ADD COLUMN parent_asin TEXT;
ALTER TABLE products ADD COLUMN sp_api_facts_json TEXT;
ALTER TABLE products ADD COLUMN sp_api_facts_at TEXT;

-- variation 통합 등록 시 parent_asin 으로 children 조회용
CREATE INDEX IF NOT EXISTS idx_products_parent_asin ON products(parent_asin) WHERE parent_asin IS NOT NULL;
