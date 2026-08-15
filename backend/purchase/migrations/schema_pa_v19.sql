-- v19: 옵션 C 통합 등록 — has_options + 옵션 추가 흔적 컬럼
--
-- 옵션 C 패턴:
--   master listing 의 채널 측 등록을 multi-option 으로 확장
--   ↳ 동일 group 의 다른 children 의 listing 은 archived
--
-- has_options=1 마킹된 listing 은 GroupDetailPage / ProductManagement 에서 🎁 표시.

ALTER TABLE listings_pa ADD COLUMN has_options INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_listings_pa_has_options
  ON listings_pa(has_options) WHERE has_options = 1;
