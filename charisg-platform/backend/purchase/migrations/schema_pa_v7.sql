-- v7: AI 배치 처리를 백그라운드 큐 방식으로 전환하기 위한 batch_jobs 테이블
CREATE TABLE IF NOT EXISTS batch_jobs (
    id          TEXT PRIMARY KEY,            -- UUID
    job_type    TEXT NOT NULL DEFAULT 'ai_detail',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | error
    total       INTEGER NOT NULL DEFAULT 0,
    processed   INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    current_product_id INTEGER,
    error_message TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    started_at  TEXT,
    finished_at TEXT
);
