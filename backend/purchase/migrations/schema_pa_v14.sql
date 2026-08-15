-- v14: sourcing_candidates 에 price_krw 컬럼 추가
--
-- 배경: Google 시트가 KR-proxy Amazon 크롤 결과로 생성되면서 가격이 KRW 로 들어오는
-- 경우가 생김. 기존 price_usd 단일 컬럼 구조에서는 통화 구분이 불가능해, KRW 값을
-- 별도 컬럼에 저장한다. 기존 row 는 price_krw=NULL 로 유지되어 영향 없음.
--
-- 소싱 → 상품관리 프로모트 시점(sourcing_promote)에서 price_usd 가 비어 있으면
-- price_krw / 환율 로 USD 대체값을 산출해 downstream (margin/customs) 은 USD 입력
-- 계약 유지.

ALTER TABLE sourcing_candidates ADD COLUMN price_krw REAL;
