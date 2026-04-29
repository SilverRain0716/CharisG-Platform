-- v13: orders 테이블에 쿠팡 주문 상세 + 아마존 발주 준비 컬럼 추가
--
-- 배경: 쿠팡 ordersheet API 응답에서 아마존 발주·통관에 필요한 필드를 흡수하고,
-- 고객 주소·이름의 영문 변환본을 저장한다. shipping_method 는 '배대지 경유'/'직배송'
-- 중 어느 쪽으로 발주했는지 사용자가 선택해 기록.
--
-- 모든 컬럼 nullable — 기존 row 영향 없음.

-- 쿠팡 원본 정보
ALTER TABLE orders ADD COLUMN customs_clearance_code TEXT;   -- 개인통관고유부호 (P170021042290)
ALTER TABLE orders ADD COLUMN orderer_real_phone     TEXT;   -- 실휴대폰 (안심번호 외)
ALTER TABLE orders ADD COLUMN shipping_message       TEXT;   -- 배송메시지 ("문 앞" 등)
ALTER TABLE orders ADD COLUMN external_sku           TEXT;   -- 쿠팡 externalVendorSkuCode (PA-2951)
ALTER TABLE orders ADD COLUMN ordered_at             TEXT;   -- 쿠팡 orderedAt (KST ISO)
ALTER TABLE orders ADD COLUMN paid_at                TEXT;   -- 쿠팡 paidAt (KST ISO)

-- 영문 변환 (LLM) — Phase B에서 채움
ALTER TABLE orders ADD COLUMN customer_name_en       TEXT;
ALTER TABLE orders ADD COLUMN address_en             TEXT;
ALTER TABLE orders ADD COLUMN translation_status     TEXT DEFAULT 'pending';  -- pending|done|error

-- 발주 경로 선택 (사용자가 아마존 결제 시 선택)
ALTER TABLE orders ADD COLUMN shipping_method        TEXT;   -- forwarder|direct (NULL = 미선택)
