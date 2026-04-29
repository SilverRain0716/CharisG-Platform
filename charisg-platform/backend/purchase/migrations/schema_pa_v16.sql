-- v16: batch_jobs.phase 컬럼 — 단계별 UI 라벨/색상 분리용
--
-- 배경: smartstore_upload 의 Phase 1(이미지 업로드) 동안 phase_message 가 갱신되지
-- 않아 사용자가 진행 상황을 볼 수 없었다. phase_message 는 사람이 읽는 한 줄 요약으로
-- 두고, 별도의 머신 친화 phase 코드를 두어 프론트가 phase 별 색/라벨을 분기할 수 있게 한다.
--
-- 값: 'phase_1'(이미지) / 'phase_1_5'(속성 추론) / 'phase_2'(등록) / 'done' / NULL
ALTER TABLE batch_jobs ADD COLUMN phase TEXT;
