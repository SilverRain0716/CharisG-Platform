-- v15: image_cache 에 네이버 CDN URL 캐시 + SHA256 해시 컬럼 추가
--
-- 배경: Phase 1(네이버 이미지 업로드)이 가장 오래 걸리는 병목. 같은 이미지가 중복
-- ASIN/시리즈/컬러 변형 등으로 여러 상품에 재사용되어도 product_id 단위 조회라 매번
-- 재업로드됨. 또한 재등록 시나리오(판매중지 → 재개)에서도 전량 재업로드.
--
-- 해결: 이미지 콘텐츠 SHA256 해시로 전역 캐시. 같은 해시의 이미지가 이미 네이버에
-- 업로드되었다면 CDN URL 재사용. 첫 업로드 시만 해시 계산 후 DB 저장.
--
-- 기존 row 는 NULL 유지 → _get_product_images 호출 시 점진적으로 채워짐.

ALTER TABLE image_cache ADD COLUMN sha256 TEXT;
ALTER TABLE image_cache ADD COLUMN naver_cdn_url TEXT;
ALTER TABLE image_cache ADD COLUMN naver_uploaded_at TEXT;

-- 교차 상품 캐시 조회용: sha256 기반으로 다른 상품에서 업로드한 URL 재사용
CREATE INDEX IF NOT EXISTS idx_image_cache_sha256 ON image_cache(sha256) WHERE sha256 IS NOT NULL;
