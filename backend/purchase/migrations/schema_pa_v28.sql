-- v28: sheet_queue.target_channels — 채널 선택 업로드 (예: 'coupang' 만)
--
-- 기존 sheet_queue 워커는 phase 4 channelsending 에서 ['smartstore','coupang']
-- 두 채널을 하드코딩으로 listings_pa 에 INSERT 했다. 네이버 한도 가득 등 사유로
-- 한 채널만 업로드하고 싶을 때 큐 단위로 채널을 지정할 수 있도록 컬럼 추가.
--
-- 값: CSV 형식 'smartstore,coupang' / 'coupang' / 'smartstore'
-- 기본값은 기존 동작과 동일하게 두 채널 모두.

ALTER TABLE sheet_queue ADD COLUMN target_channels TEXT NOT NULL DEFAULT 'smartstore,coupang';
