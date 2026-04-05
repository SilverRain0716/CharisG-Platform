# Dropshipping Dashboard Specification v1.0

> **Project**: Charis G Amazon US FBM Dropshipping
> **Updated**: 2026-04-05
> **Audience**: 프론트엔드 기획자/개발자
> **Purpose**: DS App 대시보드 개발을 위한 비즈니스 컨텍스트, 데이터 구조, UI 요구사항

---

## 1. 비즈니스 컨텍스트

### 사업 모델
Amazon US FBM 드랍쉬핑. CJ US 창고 소싱 → 아마존 미국 소비자 직배송. 무재고.

### Phase 전략

| Phase | 매출 | 예산 | 핵심 활동 |
|-------|------|------|----------|
| Phase 0 (현재) | $0~$500 | $39.99/월 | 오가닉 판매, 스코어링 검증, 42건 리스팅 |
| Phase 1 | $500~$2K | +Keepa $19/월 +PPC | Gap Score 자동화, 100+ 리스팅 |
| Phase 2 | $2K+ | 스케일 | Brand Registry, A+ Content |

---

## 2. 스코어링 모델 (★ 대시보드 핵심)

### 2.1 파이프라인
```
CJ 38K → Collected 6,200 → Hard Filter 335 → GO 107 → Listed 42 → Active
```

### 2.2 Hard Filter (8개 AND)

| # | 필터 | 조건 | 탈락 코드 |
|---|------|------|----------|
| 1 | US 창고 | us_warehouse = True | no_us_warehouse |
| 2 | 실질 마진 | >= 25% | low_margin |
| 3 | 재고 | >= 10 | low_stock |
| 4 | 가격대 | $15~$70 | price_range |
| 5 | 무게 | <= 2,000g | overweight |
| 6 | 이미지 | >= 3 | few_images |
| 7 | 브랜드 제외 | BLOCKED_BRANDS 아님 | blocked_brand |
| 8 | 카테고리 제외 | Health/Clothing 아님 | blocked_category |

### 2.3 스코어링 구조

**2축 매트릭스 (Demand × Margin):**
- A >= 0.65, B 0.40~0.64, C < 0.40 (Demand)
- A >= 0.50, B 0.25~0.49, C < 0.25 (Margin)
- 9개 그룹(AA~CC). 전략: AA/AB 즉시리스팅, CA~CC 탈락.

**3축 곱셈 정렬:** sort_score = round(D × G × M, 3)

**Demand Score** = (Category Demand × 0.5) + (Google Trends × 0.5)
**Gap Score** = review_gap(0.45) + price_position(0.35) + fbm_ratio(0.20)
**Margin Score** = margin_base × price_factor

### 2.4 GO 판정 (Amazon p75 기준)
- GO: adjusted_margin >= 25%
- GO_ORGANIC: 15~24%
- SKIP: < 15%

### 2.5 가격 포지션
- competitive: our_price <= amazon_p75
- premium: p75 < our_price <= max
- exceeded: our_price > max

---

## 3. 데이터 모델

### 3.1 dropshipping.db 테이블 (6개)

| 테이블 | 용도 | 갱신 주기 |
|--------|------|----------|
| collected_products | CJ 상품 + 스코어링 | 수집 시 |
| amazon_search_results | 키워드별 아마존 개별 리스팅 | 2주(전수)/주1(GO만) |
| amazon_search_agg | 키워드별 집계 (가격분포, 리뷰, FBM) | 상동 |
| account_health | 계정 건강도 (Phase 0: 수동) | 수동 입력 |
| listings | 리스팅 상태 | 변경 시 |
| sales | 판매 데이터 | Phase 1+ |

### 3.2 collected_products 주요 필드

**CJ 소스**: pid, productNameEn, source_price, calculated_price, shipping_cost, us_warehouse, stock_quantity, weight_g, image_count, categoryName

**아마존 크롤링**: amazon_price_p75, price_position, adjusted_margin_pct, search_keyword

**스코어링**: demand_score/grade, gap_score/grade, margin_score/grade, matrix_group, sort_score, go_decision, hard_filter_pass, filter_fail_reason

**상태**: status(candidate/listed/active/paused), tier(tier1/tier2), url

### 3.3 amazon_search_agg 필드
keyword, price_min/p25/median/p75/max, avg_review_count, min_review_count, fbm_count, total_results, collected_at

### 3.4 account_health 필드
odr(<1%/목표<0.5%), late_shipment_rate(<4%/<2%), cancel_rate(<2.5%/<1%), valid_tracking_rate(>95%/>99%), input_type(manual/auto), updated_at

---

## 4. API 엔드포인트 (DS API, port 8001)

| Method | Endpoint | 설명 |
|--------|----------|------|
| GET | /api/ds/summary | Hub용 요약 KPI |
| GET | /api/ds/dashboard | 퍼널+KPI+알림 전체 |
| GET | /api/ds/scoring/matrix | 3×3 히트맵 |
| POST | /api/ds/scoring/run | 스코어링 실행 |
| GET | /api/ds/scoring/history | 실행 이력 |
| GET | /api/ds/products | 상품 목록 (필터/정렬/페이징) |
| GET | /api/ds/products/kanban | 칸반 데이터 |
| PATCH | /api/ds/products/:id/status | 상태 변경 |
| GET | /api/ds/crawler/status | 크롤러 상태 |
| POST | /api/ds/crawler/run | 크롤러 실행 |
| GET | /api/ds/crawler/logs | 실행 로그 (SSE) |
| GET | /api/ds/monitor/health | 계정 건강도 |
| POST | /api/ds/monitor/health | 건강도 수동 입력 |
| GET | /api/ds/settings/filters | Hard Filter 설정 |
| GET | /api/ds/settings/brands | 차단 브랜드 |
| PUT | /api/ds/settings/brands | 차단 브랜드 수정 |
| GET | /api/ds/fees/:category | Referral Fee 조회 |

---

## 5. 대시보드 8개 뷰 (Performance는 placeholder)

### View 1: Pipeline Overview
퍼널(CJ 38K→Active) + KPI 4개(GO수, 평균마진, 리스팅진행률, 계정건강도) + 활동 피드

### View 2: Scoring Dashboard
3×3 히트맵 + 축별 히스토그램 + 스코어링 실행 + Hard Filter 탈락 분포 바차트

### View 3: Product List
DataTable. 13개 컬럼. sort_score 내림차순. CSV 내보내기. 벌크 상태 변경.

### View 4: Price Competitiveness
산점도(X=p75, Y=CJ, 대각선=동일가). 가격 포지션 파이차트.

### View 5: Listing Status (칸반)
4열. Candidate→Listed→Active→Paused. 드래그앤드롭.

### View 6: Account Health
게이지 4개. 3색(녹/주/빨). Phase 0 수동입력 뱃지.

### View 7: Crawler Management
크롤러 상태 카드 3개(CJ/Amazon/Trends). 실행 버튼. SSE 실시간 로그 + 진행률.

### View 8: Settings
Hard Filter 설정, 차단 브랜드/카테고리, 건강도 수동 입력, 크롤러 설정.

### Performance (Phase 1+ placeholder)
목표 $500 프로그레스. 빈 차트 프레임.

---

## 6. DS 전용 서비스 (5개)

| 파일 | 설명 |
|------|------|
| scoring_service.py | 3축 스코어링 (DS 전용) |
| cj_service.py | CJ API + Hard Filter 8개 |
| google_trends_service.py | Google Trends |
| amazon_keyword_crawler.py | Gap Score 크롤러 |
| amazon_fee_service.py | Referral Fee 매핑 |

### 크롤러 차단 방지 설정 (하드코딩)
- 요청 간 15-25초 랜덤 딜레이
- 50 키워드마다 2-3분 쿨다운
- CAPTCHA 감지 시 5분 대기
- 연속 실패 3회→10분, 5회→중단
- User-Agent 로테이션
- Webshare 프록시 US 10 IP (.env)

---

## 7. 뷰 개발 우선순위

| 순위 | 뷰 |
|------|-----|
| 1 | Pipeline Overview |
| 2 | Product List |
| 3 | Scoring Dashboard |
| 4 | Listing Status 칸반 |
| 5 | Price Competitiveness |
| 6 | Account Health |
| 7 | Crawler Management |
| 8 | Settings |

---

## 8. 현재 수치 (2026-04-05)

```
CJ US 총 상품: ~38,000 / 수집: ~6,200 / Filter 통과: 335
GO(25%+): 81 / GO_ORGANIC(15-24%): 26 / Total GO: 107
고마진(40%+) GO: 42 (Tier 1: 10, Tier 2: 32)
카테고리: 10개 / 셀러: CharisG, OTP 대기 중
```
