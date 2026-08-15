-- v36: Streaming pipeline Phase 1 — 데이터 모델 + 모니터링 (2026-05-12)
--
-- 각 product 의 단계(stage) 진행을 단일 row 로 추적. 락/타임아웃/재시도/
-- 메트릭 모두 포함. Phase 1 은 기존 sheet_queue_worker 가 이중 쓰기로
-- pipeline_items.stage 를 advance 시키는 형태. Phase 2+ 에서 streaming
-- worker 가 stage 기반 pull 처리로 전환.
--
-- 설계 문서: 사용자 요청 streaming pipeline v2 (2026-05-12)

-- ── pipeline_items ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pipeline_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_queue_id INTEGER REFERENCES sheet_queue(id),
    asin TEXT NOT NULL,

    -- FSM stage
    stage TEXT NOT NULL DEFAULT 'imported',
    -- 가능값: imported / promote_done / ai_done / channel_send_done /
    --         upload_done / kr_verify_done / forwarder_done / coupon_done /
    --         done / failed / excluded

    -- 외부 시스템 매핑 (각 단계 결과)
    sourcing_candidate_id INTEGER,
    product_id INTEGER,
    listing_id INTEGER,
    vendor_item_id INTEGER,

    -- 락 / 진행 추적
    processing_lock TEXT,                  -- worker uuid (잡 중 set)
    processing_lock_at TEXT,               -- lock 시각 (timeout 판정)
    stage_started_at TEXT,                 -- 현재 stage 진입 시각
    last_progress_at TEXT NOT NULL DEFAULT (datetime('now')),

    -- 에러 / 재시도
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    next_retry_at TEXT,                    -- exponential backoff (NULL 이면 즉시 pickable)

    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(asin, sheet_queue_id)
);

CREATE INDEX IF NOT EXISTS idx_pi_stage_pickable
    ON pipeline_items(stage, processing_lock, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_pi_progress
    ON pipeline_items(last_progress_at);
CREATE INDEX IF NOT EXISTS idx_pi_sid
    ON pipeline_items(sheet_queue_id);
CREATE INDEX IF NOT EXISTS idx_pi_asin
    ON pipeline_items(asin);

-- ── pipeline_metrics — DB 락/throughput 측정 ───────────────
CREATE TABLE IF NOT EXISTS pipeline_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    chunk_size INTEGER NOT NULL,
    items_processed INTEGER NOT NULL DEFAULT 0,
    items_failed INTEGER NOT NULL DEFAULT 0,
    api_time_ms INTEGER NOT NULL DEFAULT 0,    -- 외부 API 호출 누적
    db_tx_time_ms INTEGER NOT NULL DEFAULT 0,  -- DB transaction 시간
    db_wait_time_ms INTEGER NOT NULL DEFAULT 0,-- busy_timeout 대기 시간
    captured_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pm_captured
    ON pipeline_metrics(captured_at);
CREATE INDEX IF NOT EXISTS idx_pm_stage_captured
    ON pipeline_metrics(stage, captured_at);

-- ── SQL Views ─────────────────────────────────────────────
DROP VIEW IF EXISTS vw_pipeline_summary;
CREATE VIEW vw_pipeline_summary AS
SELECT
    sheet_queue_id,
    stage,
    COUNT(*) AS cnt,
    MIN(last_progress_at) AS oldest_progress,
    MAX(last_progress_at) AS newest_progress
  FROM pipeline_items
 GROUP BY sheet_queue_id, stage;

DROP VIEW IF EXISTS vw_pipeline_stuck;
CREATE VIEW vw_pipeline_stuck AS
SELECT
    pi.id, pi.asin, pi.stage, pi.retry_count, pi.error_message,
    pi.last_progress_at, pi.processing_lock, pi.processing_lock_at,
    sq.sheet_label, pi.sheet_queue_id,
    CAST((julianday('now') - julianday(pi.last_progress_at)) * 24 * 60 AS INTEGER) AS stuck_min
  FROM pipeline_items pi
  LEFT JOIN sheet_queue sq ON sq.id = pi.sheet_queue_id
 WHERE pi.stage NOT IN ('done','failed','excluded')
   AND datetime(pi.last_progress_at) < datetime('now','-30 minutes');

DROP VIEW IF EXISTS vw_pipeline_throughput;
CREATE VIEW vw_pipeline_throughput AS
SELECT
    stage,
    SUM(items_processed) AS done_1h,
    SUM(items_failed) AS failed_1h,
    AVG(api_time_ms * 1.0 / NULLIF(chunk_size,0)) AS avg_api_per_item_ms,
    SUM(chunk_size) AS chunks_total_items
  FROM pipeline_metrics
 WHERE datetime(captured_at) >= datetime('now','-1 hour')
 GROUP BY stage;

DROP VIEW IF EXISTS vw_db_lock_health;
CREATE VIEW vw_db_lock_health AS
SELECT
    stage,
    AVG(db_wait_time_ms) AS avg_wait_ms,
    MAX(db_wait_time_ms) AS max_wait_ms,
    AVG(db_tx_time_ms) AS avg_tx_ms,
    MAX(db_tx_time_ms) AS max_tx_ms,
    COUNT(*) AS chunks_1h
  FROM pipeline_metrics
 WHERE datetime(captured_at) >= datetime('now','-1 hour')
 GROUP BY stage;
