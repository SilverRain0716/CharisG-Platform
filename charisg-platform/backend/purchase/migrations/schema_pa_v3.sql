-- Purchase Agent DB schema v3
-- 시트 import 워크플로우: Claude 웹 프로젝트 (v3.1 프롬프트) 가 Google 시트에
-- 출력하는 10컬럼 데이터를 sourcing_candidates 에 그대로 저장하기 위한 컬럼 3개 추가.
--   1) monthly_sales  TEXT  — "3K+" 같은 표시형 문자열 그대로 저장
--   2) category       TEXT  — Amazon 카테고리 경로
--   3) notes          TEXT  — 특이사항 (v3.1 프롬프트의 "가" 컬럼)

ALTER TABLE sourcing_candidates ADD COLUMN monthly_sales TEXT;
ALTER TABLE sourcing_candidates ADD COLUMN category TEXT;
ALTER TABLE sourcing_candidates ADD COLUMN notes TEXT;
