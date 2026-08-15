-- v21: batch_jobs 에 parent_asin 컬럼 추가 (group_backfill 잡 그룹별 매칭용)
ALTER TABLE batch_jobs ADD COLUMN parent_asin TEXT;
CREATE INDEX IF NOT EXISTS idx_batch_jobs_parent_asin ON batch_jobs(parent_asin);
