-- v27: sheet_queue (대량 import 자동 파이프라인)
--
-- 사용자가 시트 URL N개 등록 → 워커가 순차 자동 처리:
--   import → promote → 상세생성 → 채널보내기 → 스마트스토어 업로드 → 쿠팡 업로드 → 디스크정리

CREATE TABLE IF NOT EXISTS sheet_queue (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_url           TEXT NOT NULL,
    sheet_label         TEXT,                              -- 사용자 표시용
    status              TEXT NOT NULL DEFAULT 'queued',
    -- queued / importing / promoting / detailing / channelsending /
    -- uploading_smartstore / uploading_coupang / cleaning / done / error / cancelled
    current_step        TEXT,                              -- "Phase 1.5 추론 중" 같은 세부 메시지
    imported            INTEGER DEFAULT 0,
    promoted            INTEGER DEFAULT 0,
    duplicates          INTEGER DEFAULT 0,
    detailed            INTEGER DEFAULT 0,
    smartstore_listed   INTEGER DEFAULT 0,
    smartstore_failed   INTEGER DEFAULT 0,
    coupang_listed      INTEGER DEFAULT 0,
    coupang_failed      INTEGER DEFAULT 0,
    error_message       TEXT,
    queued_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at          TEXT,
    finished_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_sheet_queue_status ON sheet_queue(status);
CREATE INDEX IF NOT EXISTS idx_sheet_queue_queued_at ON sheet_queue(queued_at);
