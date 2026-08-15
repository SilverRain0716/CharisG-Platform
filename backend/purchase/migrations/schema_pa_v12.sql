-- v12: 쿠팡 승인 요청(approval request) 추적용 컬럼
-- requested=False 로 임시저장된 상품은 별도 PUT /requests/approval 호출이 필요.
-- 이 컬럼이 NULL 이면 아직 승인 요청 안 한 listed 상품.
ALTER TABLE listings_pa ADD COLUMN approval_requested_at TEXT;
