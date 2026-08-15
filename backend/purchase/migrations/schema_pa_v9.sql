-- v9: 등록 전 완성 파이프라인 — 속성 AI 추론 캐시 + batch_jobs 진행 메시지
ALTER TABLE products ADD COLUMN inferred_attributes_json TEXT;
ALTER TABLE batch_jobs ADD COLUMN phase_message TEXT;
