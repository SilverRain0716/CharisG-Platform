-- v5: AI 처리 파이프라인 — products SEO 컬럼 + detail_pages HTML/마켓/플랫폼

-- products: SEO 컬럼
ALTER TABLE products ADD COLUMN seo_title TEXT;
ALTER TABLE products ADD COLUMN seo_tags TEXT;

-- detail_pages: HTML 생성 결과 + 마켓/플랫폼 구분
ALTER TABLE detail_pages ADD COLUMN html_content TEXT;
ALTER TABLE detail_pages ADD COLUMN market TEXT DEFAULT 'KR';
ALTER TABLE detail_pages ADD COLUMN platform TEXT DEFAULT 'smartstore';
