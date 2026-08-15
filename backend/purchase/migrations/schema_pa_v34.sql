-- v34: listings_pa 한국 직배송 검증 결과 영구 저장 (2026-05-05)
--
-- promote 단계(sourcing_promote.py)는 건드리지 않고, 별도 트리거(routers/kr_shipping.py)로
-- 이미 등록된 active 상품의 한국 직배 가능 여부를 주기 검증해서 캐시한다.
-- 1=가능, 0=불가, NULL=미확인.

ALTER TABLE listings_pa ADD COLUMN kr_shipping_eligible INTEGER;
ALTER TABLE listings_pa ADD COLUMN kr_shipping_checked_at TEXT;

CREATE INDEX IF NOT EXISTS idx_listings_pa_kr_shipping
    ON listings_pa(kr_shipping_eligible, kr_shipping_checked_at);
