-- v22: variation_groups 에 AI 매핑된 mandatory attribute 캐시
ALTER TABLE variation_groups ADD COLUMN mandatory_attrs_json TEXT;
