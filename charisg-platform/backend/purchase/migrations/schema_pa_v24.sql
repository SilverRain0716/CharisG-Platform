-- v24: Naver / Coupang 속성 컬럼 분리
--
-- 배경: products.inferred_attributes_json 한 컬럼에 두 채널의 다른 형태가 섞여 들어가
-- 있어 (Naver list[dict] vs Coupang {"coupang_attrs": dict}) 채널 등록 시 형태 충돌.
-- 668건 Coupang dict 가 Naver 페이로드에 그대로 주입돼 attributeValueSeq NotNull
-- 400 대량 발생 (2026-04-27).
--
-- 해결: 채널별 전용 컬럼으로 분리. inferred_attributes_json 은 호환·롤백용으로 유지.

ALTER TABLE products ADD COLUMN naver_attributes_json TEXT;
ALTER TABLE products ADD COLUMN coupang_attributes_json TEXT;

-- 기존 데이터 이주 ──────────────────────────────────────────────

-- (1) list 형태 ([{...}, ...]) → Naver 컬럼
UPDATE products
   SET naver_attributes_json = inferred_attributes_json
 WHERE inferred_attributes_json LIKE '[%';

-- (2) {"coupang_attrs": {...}} 형태 → Coupang 컬럼 (inner dict 만 저장)
UPDATE products
   SET coupang_attributes_json = json_extract(inferred_attributes_json, '$.coupang_attrs')
 WHERE inferred_attributes_json LIKE '%"coupang_attrs"%'
   AND json_extract(inferred_attributes_json, '$.coupang_attrs') IS NOT NULL;
