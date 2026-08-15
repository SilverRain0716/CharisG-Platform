-- v32: 쿠팡 위너 모니터링 (2026-05-04)
--
-- 쿠팡 OPEN API 가 위너 정보를 직접 노출하지 않아, 주문 데이터 + 등록일 기반
-- 간접 추정 시스템 도입.
--
-- winner_status 값:
--   too_new        : 등록 7일 미만 (판단 보류)
--   winner_likely  : 최근 30일 주문 1건 이상
--   suspect_loser  : 등록 후 30일 경과 + 주문 0건
--   no_data        : 미확인 또는 NULL

ALTER TABLE listings_pa ADD COLUMN winner_status TEXT;
ALTER TABLE listings_pa ADD COLUMN winner_checked_at TEXT;
ALTER TABLE listings_pa ADD COLUMN last_order_at TEXT;
ALTER TABLE listings_pa ADD COLUMN days_listed INTEGER;
ALTER TABLE listings_pa ADD COLUMN order_count_30d INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_listings_pa_winner_status
    ON listings_pa(channel, status, winner_status);
