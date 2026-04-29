-- v20: 네이버 smartstoreChannelProductNo 저장 컬럼 추가
-- channel_product_id 는 originProductNo (API 호출용),
-- smartstore_channel_no 는 셀러센터 화면 표시 ID (검색·매핑용)
ALTER TABLE listings_pa ADD COLUMN smartstore_channel_no TEXT;
