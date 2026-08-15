-- v30: orders cancel 컬럼 추가 — 쿠팡 반품/취소 sync 지원
--
-- 쿠팡 v6 returnRequests + v5 ordersheet canceled 필드 반영용.
-- - canceled         : 모든 orderItem 이 canceled=true 면 1
-- - cancel_count     : v5 cancelCount 합계 (취소 확정)
-- - hold_count_for_cancel : v5 holdCountForCancel 합계 (환불 대기 중)
-- - cancel_receipt_id: v6 returnRequests.receiptId (cancel 추적용)
-- - cancel_status    : v6 receiptStatus (RU/UC/CC/PR/...)
-- - cancel_reason    : v6 reasonCodeText / cancelReason
-- - cancel_at        : v6 createdAt (취소 접수시각)
-- - cancel_type      : v6 receiptType (CANCEL or RETURN) / null

ALTER TABLE orders ADD COLUMN canceled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN cancel_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN hold_count_for_cancel INTEGER NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN cancel_receipt_id INTEGER;
ALTER TABLE orders ADD COLUMN cancel_status TEXT;
ALTER TABLE orders ADD COLUMN cancel_reason TEXT;
ALTER TABLE orders ADD COLUMN cancel_at TEXT;
ALTER TABLE orders ADD COLUMN cancel_type TEXT;

CREATE INDEX IF NOT EXISTS idx_orders_canceled ON orders(canceled);
CREATE INDEX IF NOT EXISTS idx_orders_cancel_receipt ON orders(cancel_receipt_id);
