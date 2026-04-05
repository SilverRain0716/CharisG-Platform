# Phase 2: 드랍쉬핑 분리 (v5)

> **목표**: DS API + dropshipping.db + DS App 독립 구현
> **선행 조건**: Phase 1 완료
> **완료 기준**: /dropshipping 접속 시 8개 뷰 동작 (Performance는 placeholder)
> **참조**: `docs/dropshipping_dashboard_spec_v1.0.md` (뷰 상세, 데이터 모델, API 전체 스펙)

---

## Task 2-0: DB 테이블 실존 확인 (★ Phase 2 시작 전 필수)

```bash
sqlite3 ~/dropship-crawler/control_tower.db ".tables"
```

- [ ] amazon_search_results — 존재? Y/N
- [ ] amazon_search_agg — 존재? Y/N

존재 시 dump, 미존재 시 schema_ds.sql에서 신규 CREATE. 어느 쪽이든 최종 6개 테이블.

---

## Task 2-1: DS API 분리 (backend/dropshipping/)

**DS 전용 서비스 5개 (→ backend/dropshipping/services/):**

| 파일 | 설명 |
|------|------|
| scoring_service.py | 3축 스코어링 (DS 전용, 공용 아님) |
| cj_service.py | CJ API + Hard Filter 8개 |
| google_trends_service.py | Google Trends 수집 |
| amazon_keyword_crawler.py | 아마존 키워드 크롤링 + Gap Score |
| amazon_fee_service.py | Referral Fee 매핑 |

**Hard Filter 8개 조건 (★ 참조: `dropshipping_dashboard_spec_v1.0.md` 섹션 2.2):**

| # | 필터 | 조건 | 탈락 코드 |
|---|------|------|----------|
| 1 | US 창고 | us_warehouse = True | no_us_warehouse |
| 2 | 실질 마진 | real_margin_pct >= 25% | low_margin |
| 3 | 재고 | stock_quantity >= 10 | low_stock |
| 4 | 가격대 | $15 <= calculated_price <= $70 | price_range |
| 5 | 무게 | weight_g <= 2,000 | overweight |
| 6 | 이미지 | image_count >= 3 | few_images |
| 7 | 브랜드 제외 | BLOCKED_BRANDS에 없음 | blocked_brand |
| 8 | 카테고리 제외 | Health/Clothing 아님 | blocked_category |

모두 AND 조건. 하나라도 미달 시 스코어링 없이 즉시 제외.

**공용 모듈 참조 (packages/backend-shared):**
ai/, amazon.py, base.py, utils/(proxy_pool, rate_limiter), pricing_service.py, detail_page_service.py, category_service.py, process.py, migrations/

**DS 전용 database.py + 마이그레이션:**
(v4와 동일)

**DS 모니터링 방향:**
(v4와 동일 — cj_service + amazon_keyword_crawler 데이터 활용)

**크롤러 차단 방지 설정 (★ 마이그레이션 시 유지 확인 필수):**

amazon_keyword_crawler.py에 하드코딩된 차단 방지 설정:

| 설정 | 값 | 위치 |
|------|-----|------|
| 요청 간 딜레이 | 15-25초 랜덤 | 코드 내 하드코딩 |
| 쿨다운 | 50 키워드마다 2-3분 | 코드 내 하드코딩 |
| CAPTCHA 대기 | 감지 시 5분 | 코드 내 하드코딩 |
| 실패 임계값 | 연속 3회 → 10분 정지, 5회 → 중단 | 코드 내 하드코딩 |
| User-Agent 로테이션 | 다중 UA 풀 | 코드 내 |
| 프록시 | Webshare 로테이팅 US (10 IP) | .env (PROXY_USER, PROXY_PASS 등) |
| IP 격리 | 셀러 계정과 크롤러 별도 IP | 인프라 설정 |

추출 시 확인 사항:
- [ ] 위 설정값이 코드에 그대로 유지되는지 확인
- [ ] .env의 프록시 인증 정보가 새 구조에서도 접근 가능한지 확인
- [ ] utils/proxy_pool.py와의 연동이 정상인지 확인

**라우터 매핑:**
(v4와 동일 — 13개 라우터)

**API 엔드포인트:**
(v4와 동일)

**DB 마이그레이션:**
(v4와 동일 — 6개 테이블, Task 2-0 결과 반영)

---

## Task 2-2: DS App 구현 (8개 뷰, Performance는 placeholder)

(v4와 동일)

---

## Task 2-3: Hub API summary 연결

(v4와 동일)

---

## Phase 2 완료 체크리스트

- [ ] Task 2-0 DB 테이블 실존 확인 완료
- [ ] packages/backend-shared를 DS API에서 정상 import (utils/, migrations/ 포함)
- [ ] scoring_service.py DS 전용 배치 확인
- [ ] **Hard Filter 8개 조건이 cj_service.py에서 정상 동작 확인**
- [ ] **크롤러 차단 방지 설정 유지 확인 (딜레이, 프록시, UA)**
- [ ] DS 전용 database.py + schema_ds.sql 적용
- [ ] dropshipping.db 6개 테이블 생성 + 데이터 정합성
- [ ] DS API (port 8001) 전체 엔드포인트 동작
- [ ] DS App 8개 뷰 동작 (Performance는 placeholder)
- [ ] Hub 대시보드 DS 카드 실제 데이터 연결
- [ ] 인증 공유 확인
