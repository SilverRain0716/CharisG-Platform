# DropShipping 모노리스 레포 스냅샷 (v1)

> **수집일**: 2026-04-05
> **대상 경로**: `/home/silverrain/projects/DropShipping/`
> **목적**: CharisG-Platform 작업지시서(Phase 2~4) 수정을 위한 참고 자료
> **방식**: 읽기 전용 조사, 원본 미수정

---

## 1. 프로젝트 구조

```
DropShipping/
├── 00_회의록/                          ← 작업 지시서, 회의록
├── 01_마스터플랜/
├── 02_워크스페이스설계/
├── 03_크롤링전략/
├── 04_아키텍처/                        ← v2 설계 문서
├── backend/                            ← FastAPI 백엔드
│   ├── config/
│   ├── crawlers/
│   ├── migrations/
│   ├── routers/                        ← 32개 라우터
│   ├── services/                       ← 39개 서비스
│   │   └── ai/                         ← 7개 AI 서비스
│   └── utils/
├── frontend/                           ← React + Vite + Tailwind
│   ├── dist/
│   └── src/
│       ├── api/
│       ├── components/
│       ├── hooks/
│       ├── pages/
│       └── stores/
├── nginx/                              ← Nginx 설정
├── prompts/                            ← 프롬프트 템플릿
├── scripts/                            ← 배포 스크립트
├── data/
│   └── naver_datalab/
├── backend/schema.sql                  ← 32개 테이블
├── requirements_*.txt                  ← 모듈별 의존성 (5개)
├── .env / .env.example
├── CLAUDE.md / README.md
└── .github/workflows/                  ← CI/CD 5개 워크플로우
```

---

## 2. backend/services/ 전체 파일 목록

### 주요 서비스 (39개 .py)

| 파일명 | 첫 줄 Docstring / 역할 |
|--------|--------|
| **ai_service.py** | 후방 호환 shim — 실제 구현은 backend/services/ai/ 패키지로 분리 |
| **amazon_fee_service.py** | Amazon US Referral Fee 계산 — CJ 카테고리 → Amazon 카테고리 매핑 |
| **category_service.py** | 네이버 카테고리 자동 매핑 — 커머스 API에서 leaf 카테고리 조회 + SQLite 캐싱 |
| **cj_service.py** | CJ Dropshipping API 서비스 — 상품 검색, 배송비, 마진 계산 |
| **competition_service.py** | 쿠팡/네이버 경쟁 가격 조사 — 웹 크롤링 + 검색 API |
| **coupang_service.py** | 쿠팡 마켓플레이스(Wing) API — HMAC-SHA256 인증 기반 |
| **cs_alert_service.py** | CS/반품 알림 자동화 — 신규 생성 시 Discord 알림 |
| **cs_service.py** | CS 처리 — 문의 분류, AI 초안 생성, 자동 응답 판정 |
| **customs_service.py** | 통관 리스크 자동 체크 — 개인통관 기준 3단계 체크 |
| **detail_page_service.py** | 상세페이지 생성 엔진 — 템플릿 변수 바인딩 → HTML 조립 |
| **github_service.py** | GitHub API 연동 — Actions 워크플로우 트리거 + 상태 조회 |
| **google_trends_service.py** | Google Trends 수집 — 미국 급상승 키워드 + 카테고리별 인기 검색어 |
| **image_service.py** | 이미지 다운로드 + 리사이즈 — 마켓별 규격 맞춤 |
| **keyword_cluster_service.py** | AI 키워드 클러스터링 — 네이버 데이터랩 + 검색광고 결합 |
| **log_service.py** | 로그 저장 + SSE 브로드캐스트 |
| **margin_calculator.py** | 셀러 마진 + 고객 총 비용 이중 트랙 계산 |
| **naver_commerce_service.py** | 네이버 커머스 API — 상품 상태 변경, 조회 |
| **naver_datalab_service.py** | 네이버 데이터랩 키워드 수집 — 카테고리별 인기검색어 TOP 500 |
| **naver_searchad_service.py** | 네이버 검색광고 API — 키워드별 월간 검색량 조회 |
| **order_receiver_service.py** | 주문 자동 수신 — 네이버 스마트스토어 + 쿠팡 폴링 |
| **order_service.py** | 주문 워크플로우 — 구매대행/드랍쉬핑 분기 처리 |
| **price_monitor.py** | 아마존 가격 변동 모니터링 + 재가격 조정 |
| **pricing_service.py** | 가격 정책 엔진 — 소싱가 → 판매가 자동 계산 |
| **progress_service.py** | 수집 작업 진행률 SSE 브로드캐스트 |
| **return_service.py** | 반품/환불 워크플로우 — 마켓별 × 모델별 정책 분기 |
| **scoring_service.py** | Phase 0 스코어링 파이프라인 v2 — 2축 곱셈 모델 |
| **sheets_service.py** | Google Sheets 연동 — 기존 크롤러/리스터와 동일 시트 |
| **shopify_service.py** | Shopify 크롤링 DB 조회 — products.db의 shopify_products 테이블 |
| **stock_monitor_service.py** | 재고·품절 모니터링 — 등록 상품의 원본 상품 재고 체크 |
| **tariff_service.py** | 관세 자동화 — AI HS코드 추정 + 관세율 조회 |
| **tracking_service.py** | 배송추적 연동 — 4구간 추적 모델 |
| **trend_service.py** | BSR 변동 감지 + 트렌드 신호 생성 |

### ai/ 하위 서비스 (7개 .py)

| 파일명 | 역할 |
|--------|------|
| **ai_client.py** | Gemini/Claude API 클라이언트 + Rate Limiter |
| **ai_prompts.py** | 프롬프트 템플릿 모음 |
| **ai_translator.py** | 번역 기능 + 캐시 |
| **ai_seo.py** | SEO 키워드 리라이팅 |
| **ai_category.py** | 카테고리 자동 매핑 |
| **ai_cs.py** | CS 초안 생성 + 상품 분석 |
| **ai_trends.py** | 트렌드 키워드 분석 + 소싱 키워드 생성 |

---

## 3. backend/routers/ 엔드포인트 목록

32개 라우터 파일, 총 143개 엔드포인트 (prefix 포함)

```
/api/auth                    (2)  POST /login, GET /me
/api/categories              (4)  POST /sync, GET /search, /match, /stats
/api/competition             (4)  POST /search, /bulk-search, /compare | GET /results
/api/coupang                 (8)  GET /categories, /category-meta | POST /list-product, /bulk-list, /list-from-queue | PUT /update-price, /update-stock | GET /product-status
/api/crawl-jobs              (9)  GET "", /{job_id}, /{job_id}/progress | POST "", /{job_id}/urls, /{job_id}/start, /{job_id}/stop | DELETE /{job_id}/urls/{url_id}, /{job_id}
/api/credentials             (8)  GET /accounts, /accounts/{account_id} | POST "", /test/{account_id}, /seed-from-env | PUT /{cred_id} | DELETE /{cred_id}, /accounts/{account_id}
/api/cs                      (13) GET /kpi, /tickets, /tickets/{ticket_id}, /templates | POST /tickets, /tickets/{ticket_id}/draft, /send, /close, /templates | PUT /templates/{tmpl_id} | DELETE /templates/{tmpl_id}, /seed-templates
/api/cs-alert                (7)  POST /check, /test, /schedule/start, /schedule/stop | GET /history, /config | PUT /config
/api/customs                 (5)  POST /check, /bulk-check | GET /blocked-items, /excluded-items | PUT /config
/api/dashboard               (4)  GET /kpi, /pipeline, /channels, /summary
/api/detail-pages            (8)  GET "", /{detail_page_id} | PUT /{detail_page_id}, /{detail_page_id}/images | POST /{detail_page_id}/preview, /ready, /draft, /regenerate
/api/import                  (3)  GET /template-excel | POST /upload-excel, /upload-products
/api/keywords                (4)  POST /cluster, /full-pipeline | GET /clusters, /top
/api/listings                (9)  GET /queue, /queue/stats, "" | POST /queue, /queue/bulk, /queue/{queue_id}/approve, /reject | PUT /queue/{queue_id} | DELETE /queue/{queue_id}
/api/logs                    (2)  GET "", /stream
/api/monitor                 (11) POST /stock-check, /price-check, /schedule-start, /schedule-stop | GET /stock-changes, /out-of-stock, /price-changes, /price-history/{asin}, /price-summary, /price-products, /config | PUT /config
/api/naver-datalab           (5)  GET /categories, /keywords, /collected, /keyword-list | POST /collect
/api/orders                  (17) GET "", /kpi, /channels, /recent, /receive-config, /schedule/status, /{order_id}, /{order_id}/workflow | POST /check-new, "", /{order_id}/workflow, /{order_id}/source, /schedule/start, /schedule/stop | PUT /receive-config, /{order_id}/tracking, /status
/api/pipeline                (3)  POST /trigger | GET /runs, /status
/api/pricing                 (11) GET "", /{policy_id}, /exchange-rate, /margin-config | POST "", /calculate, /calculate-margin, /bulk-calculate | PUT /{policy_id}, /margin-config | DELETE /{policy_id}
/api/process                 (9)  POST /translate, /translate-product/{product_id}, /seo, /seo-product/{product_id}, /category/{product_id}, /images/{product_id}, /detail-page, /bulk-process | GET /{product_id}/preview
/api/products                (9)  GET "", /collected, /collected/stats, /shopify/stats, /shopify/brands | POST /scoring/run | GET /scoring/candidates, /scoring/report
/api/returns                 (10) GET /kpi, /policies, /refunds, "" | POST /policies, "", /{return_id}/process | DELETE /policies/{policy_id} | POST /refunds/{refund_id}/complete | GET /{return_id}
/api/schedules               (2)  GET "" | PATCH /{schedule_id}/toggle
/api/search-ad               (3)  GET /search-volume, /results | POST /bulk-search-volume, /collect-from-datalab
/api/shipping                (4)  GET /kpi, /by-channel, /issues, /issues/summary, /settings
/api/shops                   (5)  GET /", /{shop_id} | POST /", /{shop_id}/test | PUT /{shop_id} | DELETE /{shop_id}
/api/tariff                  (5)  POST /classify, /bulk-classify, /update-db | GET /lookup, /db-status
/api/templates               (4)  GET "" | POST "" | PUT /{tpl_id} | DELETE /{tpl_id}
/api/tracking                (7)  GET /{order_id}, /delayed, /config | POST /refresh/{order_id}, /refresh-all | PUT /{order_id}/manual, /config | POST /schedule/start, /schedule/stop
/api/trends                  (13) GET /signals, /bsr-history, /categories, /google-trends, /google-trends/related, /google-trends/interest | POST /detect, /ai-sourcing, /ai-sourcing/register, /ai-sourcing/cj-search, /full-sourcing
```

---

## 4. backend/schema.sql CREATE TABLE 목록 (32개)

```
1.  users
2.  channels
3.  credentials
4.  pipeline_runs
5.  logs
6.  orders
7.  issues
8.  templates
9.  schedules
10. schema_version
11. shops
12. pricing_policies
13. trend_signals
14. crawl_jobs
15. crawl_job_urls
16. import_mappings
17. import_jobs
18. collected_products
19. product_images
20. detail_pages
21. translation_cache
22. listing_queue
23. listings
24. order_sourcing
25. order_workflows
26. cs_tickets
27. cs_responses
28. cs_templates
29. return_policies
30. return_requests
31. refunds
```

---

## 5. .env / .env.example 키 목록

### .env.example (실제값 템플릿)

```
CTRL_ADMIN_USER
CTRL_ADMIN_PASS
CTRL_SECRET_KEY
CTRL_MASTER_KEY
CTRL_DB_PATH
CTRL_CORS_ORIGINS
CJ_EMAIL
CJ_PASSWORD
CJ_API_KEY
NAVER_CLIENT_ID
NAVER_CLIENT_SECRET
SHEET_ID
GOOGLE_SA_KEY_PATH
GEMINI_API_KEY
GITHUB_TOKEN
GITHUB_REPO
DISCORD_WEBHOOK_URL
PROXY_HOST
PROXY_PORT
PROXY_USER_BASE
PROXY_PASSWORD
```

### .env (실제 운영 환경값 — 민감 정보 포함)

```
PROXY_HOST, PROXY_PORT, PROXY_USER_BASE, PROXY_PASSWORD
GOOGLE_SA_KEY_PATH, SHEET_ID
CTRL_ADMIN_USER, CTRL_ADMIN_PASS, CTRL_SECRET_KEY, CTRL_AUTH_BYPASS, CTRL_MASTER_KEY
GITHUB_TOKEN, GITHUB_REPO
NAVER_CLIENT_ID, NAVER_CLIENT_SECRET
CJ_EMAIL, CJ_PASSWORD, CJ_API_KEY
GEMINI_API_KEY
DISCORD_WEBHOOK_URL
NAVER_SEARCHAD_API_KEY, NAVER_SEARCHAD_SECRET_KEY, NAVER_SEARCHAD_CUSTOMER_ID
COUPANG_VENDOR_ID, COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY
ETSY_API_KEY, ETSY_SHARED_SECRET, ETSY_SHOP_ID, ETSY_ACCESS_TOKEN, ETSY_REFRESH_TOKEN
NAVER_SEARCH_CLIENT_ID, NAVER_SEARCH_CLIENT_SECRET
```

**차이점**: .env에는 Etsy, 네이버 검색, 쿠팡 키가 추가되어 있음 (운영용 추가 채널)

---

## 6. Python 의존성

### requirements_backend.txt (FastAPI 백엔드)

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
python-jose[cryptography]==3.3.0
bcrypt==4.1.2
python-dotenv==1.0.1
python-multipart==0.0.18
requests==2.32.3
gspread==6.1.2
google-auth==2.29.0
cryptography==44.0.0
openpyxl==3.1.5
Pillow==11.1.0
pytrends==4.9.2
playwright==1.45.0
httpx==0.28.1
```

### requirements_cj.txt (CJ 크롤러)

```
requests==2.32.3
urllib3==2.2.1
python-dotenv==1.0.1
gspread==6.1.2
google-auth==2.29.0
google-auth-oauthlib==1.2.0
```

### requirements_ebay.txt (eBay 크롤러)

```
playwright==1.44.0
gspread==6.1.2
google-auth==2.29.0
tenacity==8.3.0
```

### requirements_smartstore.txt (스마트스토어 리스터)

```
requests==2.31.0
bcrypt==4.1.2
pybase64==1.4.0
gspread==6.0.2
google-auth==2.28.0
python-dotenv==1.0.1
Pillow==10.2.0
```

### requirements_spocket.txt (Spocket 크롤러)

```
playwright==1.44.0
gspread==6.1.2
google-auth==2.29.0
python-dotenv==1.0.1
requests==2.31.0
```

---

## 7. package.json dependencies (프론트엔드)

### frontend/package.json

**Dependencies**:
- react: ^18.3.1
- react-dom: ^18.3.1
- react-router-dom: ^6.28.0
- zustand: ^5.0.2
- recharts: ^2.15.0
- @tanstack/react-query: ^5.62.0

**DevDependencies**:
- @types/react: ^18.3.12
- @vitejs/plugin-react: ^4.3.4
- autoprefixer: ^10.4.20
- postcss: ^8.4.49
- tailwindcss: ^3.4.16
- vite: ^6.0.3

---

## 8. GitHub Actions 워크플로우

### .github/workflows/cj_crawl.yml

```yaml
name: CJ Dropshipping Crawler
on: workflow_dispatch
jobs:
  crawl:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - Checkout 코드
      - Python 3.11 설정 + pip 캐시
      - 패키지 설치 (requirements_cj.txt)
      - GCP 인증 파일 base64 디코딩
      - CJ 크롤러 실행 (python cj_crawler.py)
      - 로그 아티팩트 업로드 (7일 보관)
      - 인증 파일 정리
```

### .github/workflows/crawl.yml

```yaml
name: eBay + Amazon Crawler
on: workflow_dispatch
jobs:
  crawl:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - EC2 SSH 접속 (52.79.177.182)
      - venv 활성화 + 크롤러 실행
        - python ebay_sold_crawler.py >> ~/logs/ebay.log
        - python amazon_best_crawler.py >> ~/logs/amazon.log
```

### .github/workflows/price_monitor.yml

```yaml
name: Amazon Price Monitor
on:
  workflow_dispatch
  schedule: "0 1,13 * * *"  # UTC 01:00, 13:00 (KST 10:00, 22:00)
jobs:
  monitor:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - Checkout + Python 3.12 설정
      - 의존성 설치
      - Python 스크립트로 가격 모니터링 실행
        (backend.database.init_db() → backend.services.price_monitor.check_prices())
      - 로그 아티팩트 업로드 (7일 보관)
```

### 기타 워크플로우

- **smartstore_list.yml**: 스마트스토어 리스팅 자동화
- **delete_and_relist.yml**: 상품 재리스팅 정책

---

## 9. CLAUDE.md (운영 가이드)

**프로젝트 개요**:
- 해외 구매대행 + 드랍쉬핑 통합 플랫폼
- 9레이어 파이프라인 (L1 판매시장 ~ L7 반품/환불)
- 판매 채널: 네이버 스마트스토어, 쿠팡(KR) / Amazon FBM(US) — Etsy 철수 (2026-04)
- 마일스톤: M0~M6 완료 (32테이블, 22라우터, 17서비스, E2E 53개)

**백엔드 명령어**:

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

**프론트엔드 명령어**:

```bash
cd frontend && npm install && npm run dev      # :5173
cd frontend && npm run build                   # production
```

**EC2 배포 (프로덕션)**:
- 호스트: 52.79.177.182 (서울, ap-northeast-2, t3.small)
- 프로젝트 경로: `/home/ubuntu/dropship-crawler`
- systemd 서비스: `charisg-api` (uvicorn), nginx
- Nginx 설정: `nginx/charisg.conf` → `/etc/nginx/sites-available/`
- API 문서: `https://52.79.177.182/api/docs`

**PENDING 작업 (우선순위순)**:
- P0: Amazon FBM 셀러 계정 셋업 (진행중)
- P1: CJ Dropshipping 주문 자동화 (크롤러/매칭만 구현)
- P2: Amazon 주문 → CJ 자동 발주 파이프라인
- P3: Amazon SP-API 리스터
- P4: 프록시 구매 + 크롤러 실행 ($3.50/월)
- P5: 스마트스토어 관부가세 (데드라인 2026-04-29)
- P6: 네이버 톡톡 챗봇 API 연동
- P7: 쿠팡 WING API 리스터, Google Sheets 완전 전환
- P8: 상세페이지 자동생성 (gemini-3.1-flash-image-preview 테스트 완료)

**알려진 이슈**:
1. Webshare 프록시 대역폭 소진 — 크롤러 실행 불가
2. Gemini API 429 — 무료 플랜 할당량 초과
3. 스마트스토어 `customsDutyInfo` 필드명 미확인
4. nginx Mixed Content — 새 location 추가 시 헤더 누락 주의
5. `.env` 중복 키 — cat >> 반복으로 발생

---

## 10. EC2/배포 설정

### nginx/charisg.conf (경로: `/etc/nginx/sites-available/charisg.conf`)

```nginx
server {
    listen 80;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;

    ssl_certificate /etc/nginx/ssl/self-signed.crt;
    ssl_certificate_key /etc/nginx/ssl/self-signed.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    root /home/ubuntu/dropship-crawler/frontend/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # SSE 로그 스트림 (버퍼링 비활성화)
    location /api/logs/stream {
        proxy_pass http://127.0.0.1:8000;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }

    # SSE 수집 작업 진행률
    location ~ ^/api/crawl-jobs/\d+/progress$ {
        proxy_pass http://127.0.0.1:8000;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }

    # 정적 파일 캐시
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff2?)$ {
        expires 7d;
        add_header Cache-Control "public, immutable";
    }
}
```

### scripts/deploy.sh (배포 스크립트)

**사용**: `bash scripts/deploy.sh [setup|update|restart]`

**세부 단계**:

1. **setup** — 초기 설정
   - 시스템 패키지 설치 (nginx, python3-pip, nodejs, npm, certbot)
   - Node.js 18+ 확인 + 업그레이드
   - Python venv 생성
   - requirements_backend.txt 설치
   - .env 파일 확인 (없으면 .env.example 복사)
   - 프론트엔드 빌드 (npm install && npm run build)
   - Nginx 자체 서명 인증서 생성
   - Nginx 설정 파일 배치 (`nginx/charisg.conf` → `/etc/nginx/sites-available/`)
   - systemd 서비스 등록 (`scripts/charisg-api.service`)
   - 대시보드 & API 문서 URL 출력

2. **update** — 코드 업데이트
   - git fetch + reset --hard origin/main
   - pip install 의존성 갱신
   - 프론트엔드 npm 빌드
   - charisg-api 서비스 재시작

3. **restart** — 서비스 재시작만

**환경 변수 확인**:
- `.env` 파일에 `CTRL_ADMIN_PASS`, `CTRL_SECRET_KEY`, `CTRL_MASTER_KEY` 설정 필수

---

## 요약

- **아키텍처**: 9레이어 파이프라인 (소싱 → 가공 → 판매 → 주문 → CS → 반품)
- **백엔드**: FastAPI + SQLite (32테이블) + JWT 인증
- **프론트엔드**: React 18 + Vite + Zustand + React Query
- **배포**: EC2 (52.79.177.182, 서울) + Nginx + systemd
- **CI/CD**: GitHub Actions (5개 워크플로우, manual + cron 기반)
- **의존성**: Python 5개 모듈별 requirements.txt + Node.js 패키지
- **상태**: M0~M6 완료, P0~P8 PENDING 작업 진행중
