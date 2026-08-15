-- v37: 그룹 등록 큐 (2026-05-30)
--
-- 시트 ASIN → 부모 dedup 후, 부모 단위로 register_new_group_listing 을 직렬 처리하는 worker 큐.
-- t3.micro 한계상 1 부모 약 30~46분 소요 (children≤55 기준). 동시 처리 금지 (DB 락 + AI rate).
-- sheet_queue (단일상품 경로) 와 별개 — 그룹/멀티옵션 전용. 같은 ASIN 이 두 큐에 중복돼도 무방
-- (sheet_queue 는 단일 등록, group_queue 는 부모 단위 묶음 등록).
--
-- 워커: backend.purchase.scripts.group_registration_worker (systemd: charisg-pa-group-worker.service)
-- 폴링 60초, 한 번에 1 부모. variation_groups 미적재 시 resync_group_from_spapi 자동 호출.

CREATE TABLE IF NOT EXISTS group_registration_queue (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_asin         TEXT NOT NULL,
    sheet_id            TEXT,                              -- 출처 시트 (optional)
    sheet_tab           TEXT,                              -- 출처 탭 (optional)
    status              TEXT NOT NULL DEFAULT 'queued',
    -- queued / pre_scanning / registering / done / error / skipped / cancelled
    requested           INTEGER NOT NULL DEFAULT 0,        -- 1=자동승인요청, 0=임시저장(기본)
    channels            TEXT NOT NULL DEFAULT 'coupang',   -- "coupang" / "smartstore" / "coupang,smartstore"

    -- pre-scan 결과
    n_splits            INTEGER,
    primary_dim         TEXT,
    children_count      INTEGER,

    -- 결과 메타 (등록 후)
    seller_product_ids  TEXT,                              -- JSON list ["16232061239", "16232061308"]
    listing_ids         TEXT,                              -- JSON list [333572, 333573]
    options_persisted   INTEGER,
    error_message       TEXT,
    skip_reason         TEXT,                              -- splits>8 / variation_groups 미적재 등

    queued_at           TEXT NOT NULL DEFAULT (datetime('now')),
    started_at          TEXT,
    finished_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_grq_status
    ON group_registration_queue(status);
CREATE INDEX IF NOT EXISTS idx_grq_queued_at
    ON group_registration_queue(queued_at);
CREATE INDEX IF NOT EXISTS idx_grq_parent_asin
    ON group_registration_queue(parent_asin);
