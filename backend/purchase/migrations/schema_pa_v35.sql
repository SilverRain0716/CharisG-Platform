-- v35: 직배 불가 상품의 배대지 경유 가격 재산정 추적 (2026-05-06)
--
-- listings_pa.kr_shipping_eligible=0 인 상품에 대해 배대지(forwarder) 운송비를
-- 무게 기반 LBS 요금표로 계산하여 마진 35% 보존 가격을 산출.
-- 인상률 따라 reprice / margin_shrink / mark_exclude 자동 분류.

ALTER TABLE listings_pa ADD COLUMN forwarder_shipping_usd REAL;          -- 요금표 lookup 결과 (USD)
ALTER TABLE listings_pa ADD COLUMN forwarder_required_price_krw INTEGER; -- 마진 35% 보존 시 필요 가격
ALTER TABLE listings_pa ADD COLUMN forwarder_action TEXT;                -- reprice | margin_shrink | mark_exclude | keep
ALTER TABLE listings_pa ADD COLUMN forwarder_processed_at TEXT;          -- 분류 처리 시각

CREATE INDEX IF NOT EXISTS idx_listings_pa_forwarder_action
    ON listings_pa(forwarder_action, kr_shipping_eligible);
