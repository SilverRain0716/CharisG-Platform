-- v17: SP-API 보강 확장 — 아마존 판매가, 크기/무게, 바코드, 카테고리
--
-- 배경: promote 시 SP-API에서 summaries+attributes+images 만 가져오던 것을
-- dimensions, identifiers, classifications 까지 확장. 배송비 산정, 관세 판단,
-- 쿠팡 바코드 등록 등에 활용.

ALTER TABLE products ADD COLUMN amazon_price_usd REAL;
ALTER TABLE products ADD COLUMN dimensions_json TEXT;
ALTER TABLE products ADD COLUMN identifiers_json TEXT;
ALTER TABLE products ADD COLUMN amazon_category_json TEXT;
