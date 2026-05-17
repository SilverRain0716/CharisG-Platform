-- v37: 마진 계산 누락 비용 보강 settings (2026-05-18)
--
-- v35 forwarder_pricing 가 calculate_sale_krw 호출 시 비용 항목이 일부 누락되고
-- 일부 중복 차감되는 문제를 코드 변경으로 수정. 본 마이그는 settings 만 추가.
--
-- 문제:
--   1. 안전마진(처리비, 5,000원) — 미차감
--   2. CS 비용 (2,000원) — Forwarder 경로인데 미차감
--   3. 리턴 적립 (3%) — Forwarder 경로인데 미차감
--   4. Forwarder 경로 sale_krw 산정 시 Amazon 직배송비($11) 도 같이 차감 (이중)
--
-- 코드 변경 (별도 commit):
--   - pricing_service_pa.calculate_sale_krw: safety/cs/return 파라미터 추가 (default 0, 하위호환)
--   - forwarder_pricing.recalculate_blocked_listings: amazon_shipping_usd=0 명시 + safety/cs/return 전달
--   - channel_listing_service.send_to_channels: 신규 등록 시도 safety/cs/return 전달
--
-- 운영 흐름 변경 없음. 운영자가 기존대로 kr_shipping_verify → forwarder_pricing 트리거.

INSERT OR IGNORE INTO settings(key, value) VALUES
    ('pricing.amazon_direct_shipping_usd', '12'),    -- Direct(kr_shipping_eligible=1) 시 flat. 현재 미사용 — 향후 가격 인하 정책 변경 시 활용.
    ('pricing.safety_margin_krw',          '5000');  -- 양쪽 경로 공통. 기존 margin.forwarder_fee_krw 의 의미를 안전마진으로 일반화.

-- 기존 키 재사용 (변경 없음):
--   margin.return_reserve_pct  (3)      ← Forwarder 전용
--   margin.cs_cost_krw         (2000)   ← Forwarder 전용
--   margin.forwarder_fee_krw   (5000)   ← 기존 — 새 키와 동일 의미. 향후 통일 검토.
--   amazon_shipping_default_usd (11)    ← prod 운영자 수동 조정. 그대로.
