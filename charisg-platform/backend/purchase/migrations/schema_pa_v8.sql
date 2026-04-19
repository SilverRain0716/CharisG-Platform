-- v8: listings_pa 실 마진/리스크 + products 속성 채널 동기 시각
-- 운영 DB(EC2)는 2026-04-17에 직접 ALTER로 적용된 상태. 이 파일은 신규 환경 init 시 동일 상태 재현용.
ALTER TABLE listings_pa ADD COLUMN net_margin_pct REAL;
ALTER TABLE listings_pa ADD COLUMN margin_risk TEXT;
ALTER TABLE products ADD COLUMN attributes_updated_at TEXT;
