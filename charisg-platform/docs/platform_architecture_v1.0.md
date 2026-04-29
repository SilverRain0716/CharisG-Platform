# Platform Architecture v1.0

> **Project**: Charis G Platform (Dropshipping + Purchase Agent)
> **Architecture**: Micro-Frontend B-3 (독립 앱 + 공유 셸)
> **Updated**: 2026-04-05

---

## 1. 아키텍처 개요

### 도메인 + 라우팅

```
{도메인}/                    → Shell App (Hub)     port 3000
{도메인}/dropshipping/*      → Dropshipping App    port 3001
{도메인}/purchase/*          → Purchase Agent App   port 3002

{도메인}/api/hub/*           → Hub API             port 8000
{도메인}/api/ds/*            → DS API              port 8001
{도메인}/api/pa/*            → PA API              port 8002
```

Nginx 리버스 프록시. 단일 도메인, 단일 SSL.

### 앱별 책임

**Shell App**: 로그인/인증, Hub 대시보드 (양쪽 요약 KPI), Global Top Bar, 앱 요약 API 프록시

**Dropshipping App**: DS 전용 8개 뷰, DS API만 호출, 독립 Zustand 스토어. 참조: `dropshipping_ia_spec_v1.0.md`, `dropshipping_dashboard_spec_v1.0.md`

**Purchase Agent App**: PA 전용 7개 메뉴, PA API만 호출, 독립 Zustand 스토어. 참조: `purchase_agent_ia_spec_v1.1.md`

---

## 2. 백엔드 분리

### API 분리

| API | 포트 | 라우터 |
|-----|------|--------|
| Hub API | 8000 | auth, summary |
| DS API | 8001 | scoring, cj, gap, trends, ds_products, ds_listings, crawler, fees, ds_monitor, ds_settings, detail_page, process, category |
| PA API | 8002 | datalab, searchad, keywords, sourcing, margin, customs, competition, pa_products, detail_page, smartstore, coupang, orders, tracking, cs, returns, pa_monitor, pa_settings |

### DB 분리

| DB | 테이블 수 | 접근 |
|----|----------|------|
| hub.db | 3 | Hub API만 |
| dropshipping.db | 6 | DS API만 |
| purchase.db | 19 | PA API만 |

business_model 기준 WHERE 절 분리. 초기 dump 후 각자 마이그레이션 러너 독립 운영.

### 공용 모듈 (packages/backend-shared/)

ai/, amazon.py, base.py, utils/(proxy_pool, rate_limiter), pricing_service.py, detail_page_service.py, category_service.py, process.py, migrations/__init__.py

**포함 안 됨**: scoring_service.py(DS 전용), database.py(각 API 전용), 모니터링 서비스(각 전용)

---

## 3. 프론트엔드 구조

### 기술 스택 (유지)

React 18.3.1 + Vite 6.0.3 + Tailwind CSS 3.4.16 + Zustand 5.0.2 + React Router DOM 6.28.0 + TanStack React Query 5.62.0 + Recharts 2.15.0

### 공유 디자인 시스템 (packages/ui/)

GlobalTopBar, Sidebar, DataTable, KPICard, FunnelChart, StatusBadge, AlertFeed, KanbanBoard + tailwind.preset.js + tokens.css

### 인증 공유 (packages/auth/)

Shell 로그인 → JWT httpOnly 쿠키 (domain={도메인}) → 서브 앱 자동 공유. 401 시 Shell 로그인 리다이렉트.

---

## 4. UI/UX 구조

### Global Top Bar (3개 앱 공통)

| 위치 | 구성 |
|------|------|
| 왼쪽 | Charis G 로고 (→Hub 복귀) |
| 중앙 | 앱 전환 탭 (Hub / Dropshipping / Purchase Agent). 활성 앱 underline. 비활성 탭에 배지(미처리 건수) |
| 탭 드롭다운 | 호버 시 해당 앱 요약 KPI 4~5개 + "Go to App →" |
| 오른쪽 | 알림 아이콘 + 프로필 |

### Hub 대시보드 (로그인 직후)

DS 요약 카드(teal) + PA 요약 카드(purple) 나란히. 각각 KPI 6개 + "Open →" 버튼.

### 앱 전환 시 동작

| 요소 | 전환 시 | 내부 탐색 시 |
|------|--------|------------|
| Top Bar | 유지 (탭만 변경) | 유지 |
| Sidebar | 전체 교체 | 활성 메뉴 변경 |
| Content | 전체 교체 | 페이지 렌더링 |
| URL | /dropshipping ↔ /purchase | 하위 경로 변경 |
| 페이지 리로드 | 발생 (독립 앱) | 없음 (SPA) |

---

## 5. 인프라

| 프로세스 | 포트 | 관리 |
|---------|------|------|
| Nginx | 80/443 | 리버스 프록시 + SSL |
| Shell App | 3000 | PM2 |
| DS App | 3001 | PM2 |
| PA App | 3002 | PM2 |
| Hub API | 8000 | PM2 + uvicorn |
| DS API | 8001 | PM2 + uvicorn |
| PA API | 8002 | PM2 + uvicorn |

EC2 최소 t3.medium 권장.

---

## 6. 마이그레이션 계획

| Phase | 내용 |
|-------|------|
| Phase 1 | Nginx + backend-shared(utils/migrations 포함) + Hub API + Shell App |
| Phase 2 | DS API(5개 전용 서비스) + dropshipping.db(6개 테이블) + DS App(8개 뷰) |
| Phase 3 | PA API(15개 전용 서비스) + purchase.db(19개 테이블) + PA App(7개 메뉴) |
| Phase 4 | 크롤러 마이그레이션(의존성/차단방지) + 통합 테스트 + 모노리스 아카이브 |
