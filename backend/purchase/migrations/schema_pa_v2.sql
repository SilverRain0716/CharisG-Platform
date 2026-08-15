-- Purchase Agent DB schema v2
-- 디스커버리 풀 파이프라인을 위한 변경:
--   1) keywords.category_cid 컬럼 추가 (데이터랩 카테고리 cid 역추적)
--   2) pa_discovery_categories — 네이버 데이터랩 카테고리 트리 + tracked flag
--   3) pa_discovery_runs — 파이프라인 실행 이력 + 단계별 진행률

-- v1 시점 keywords 테이블에 카테고리 연결이 없었으므로 멱등 추가
-- (MigrationRunner 가 schema_meta 로 version 추적 → 재실행 안됨)
ALTER TABLE keywords ADD COLUMN category_cid INTEGER;

CREATE TABLE IF NOT EXISTS pa_discovery_categories (
    cid          INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    full_path    TEXT NOT NULL,
    level        INTEGER NOT NULL,
    parent_cid   INTEGER,
    tracked      INTEGER NOT NULL DEFAULT 0,
    last_synced  TEXT
);

CREATE TABLE IF NOT EXISTS pa_discovery_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',   -- running|done|failed
    current_stage TEXT,                              -- init|categories|rank|searchad|trend|cluster|done
    stage_log     TEXT,                              -- JSON: 단계별 진행 상태와 카운트
    error         TEXT,
    inserted_kw   INTEGER DEFAULT 0,
    updated_kw    INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pa_discovery_runs_started ON pa_discovery_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pa_discovery_categories_tracked ON pa_discovery_categories(tracked);
