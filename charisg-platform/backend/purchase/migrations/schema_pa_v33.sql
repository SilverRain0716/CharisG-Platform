-- v33: 쿠팡 셀러 상태 동기화 (2026-05-04)
--
-- listings_pa 에 쿠팡 측 statusName/status 캐싱.
-- 쿠팡 statusName: 임시저장/저장중/승인대기중/승인완료/부분승인완료/승인반려/판매중지/거래정지

ALTER TABLE listings_pa ADD COLUMN coupang_seller_status TEXT;       -- APPROVED, REJECTED, REQUEST 등
ALTER TABLE listings_pa ADD COLUMN coupang_status_name TEXT;          -- 한글: 승인완료, 승인반려 등
ALTER TABLE listings_pa ADD COLUMN coupang_status_synced_at TEXT;

CREATE INDEX IF NOT EXISTS idx_listings_pa_coupang_status
    ON listings_pa(channel, coupang_seller_status);
