-- v31: 클린 위반 이력 로그 테이블 (2026-05-04)
--
-- 네이버 스마트스토어 클린 프로그램 위반 방지 시스템.
-- 소싱→AI→업로드 3중 게이트에서 검출된 위반 이력 기록.
--
-- stage:           sourcing | ai | upload_smartstore | upload_coupang
-- violation_type:  prohibited_ingredient | efficacy_claim | duplicate_asin
--                  prohibited_category | invalid_attribute
-- action_taken:    blocked | sanitized | excluded

CREATE TABLE IF NOT EXISTS clean_violation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    stage TEXT NOT NULL,
    violation_type TEXT NOT NULL,
    action_taken TEXT NOT NULL,
    matched_keyword TEXT,
    product_id INTEGER,
    asin TEXT,
    channel TEXT,
    original_text TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_clean_violation_log_product
    ON clean_violation_log(product_id);
CREATE INDEX IF NOT EXISTS idx_clean_violation_log_detected
    ON clean_violation_log(detected_at);
CREATE INDEX IF NOT EXISTS idx_clean_violation_log_stage
    ON clean_violation_log(stage, violation_type);
