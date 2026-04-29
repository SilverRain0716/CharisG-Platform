# CLAUDE.md — Charis G Platform (v5)

## 프로젝트 개요

Charis G는 구매대행(Purchase Agent)과 드랍쉬핑(Dropshipping) 두 개의 독립된 이커머스 시스템을 하나의 플랫폼으로 운영하는 프로젝트입니다.

현재 모노리스 SPA를 마이크로 프론트엔드 아키텍처(B-3)로 전환합니다.

## 아키텍처 (B-3)

```
{도메인}/                       → Shell App (Hub)     port 3000
{도메인}/dropshipping/*         → Dropshipping App    port 3001
{도메인}/purchase/*             → Purchase Agent App   port 3002

{도메인}/api/hub/*              → Hub API             port 8000
{도메인}/api/ds/*               → DS API              port 8001
{도메인}/api/pa/*               → PA API              port 8002
```

## 기술 스택 (유지)

- Frontend: React 18.3.1 + Vite 6.0.3 + Tailwind CSS 3.4.16
- 상태관리: Zustand 5.0.2 / 서버 상태: TanStack React Query 5.62.0
- 라우터: React Router DOM 6.28.0 / 차트: Recharts 2.15.0
- Backend: FastAPI + uvicorn / DB: SQLite / 서버: EC2 Ubuntu

## 핵심 규칙

1. **분리 원칙**: apps/dropshipping/은 절대 apps/purchase/를 import하지 않음.
2. **백엔드 공용 모듈**: `packages/backend-shared/`에 두고 `pip install -e`로 참조.
3. **스코어링은 공용이 아님**: DS(3축 곱셈 + Hard Filter 8개)와 PA(2축 + 통관 필터)는 완전히 다른 로직.
4. **모니터링도 공용이 아님**: DS(CJ 재고 + Amazon p75)와 PA(네이버/쿠팡 리스팅 + 아마존 가격→마진)는 대상과 로직이 다름.
5. **database.py는 각 API 전용**: DB 경로가 다르므로 커넥션 관리는 각자. 마이그레이션 러너 로직만 backend-shared에서 공유.
6. **디자인 통일**: 3개 앱 모두 packages/ui/ 공유 디자인 시스템.
7. **인증 공유**: Shell → JWT httpOnly 쿠키 → 서브 앱 자동 공유.
8. **frontend-design 스킬**: Anthropic 공식 스킬 적용.

## 현재 코드베이스 (모노리스)

위치: `~/dropship-crawler/` (EC2)

(v4와 동일 — 전체 파일 트리 + [공용]/[DS 전용]/[PA 전용] 분류)

## 공용 모듈 분리 전략

### packages/backend-shared/ 에 포함

| 모듈 | 사용처 |
|------|--------|
| ai/ | DS + PA |
| amazon.py | DS + PA |
| base.py | DS + PA |
| utils/proxy_pool.py | DS + PA |
| utils/rate_limiter.py | DS + PA |
| pricing_service.py | DS + PA |
| detail_page_service.py | DS + PA |
| category_service.py | DS + PA |
| process.py | DS + PA |
| migrations/__init__.py | DS + PA (러너 로직만) |

### 각 API 전용 (포함 안 됨)

(v4와 동일)

### DB/마이그레이션 전략

(v4와 동일)

## DB 분리

### hub.db (3개 테이블)
(v4와 동일)

### dropshipping.db (6개 테이블)
| 테이블 | 분리 방법 | 비고 |
|--------|----------|------|
| collected_products | WHERE business_model='dropship' | |
| amazon_search_results | 존재 시 dump, 없으면 신규 CREATE | Phase 2 Task 2-0에서 확인 |
| amazon_search_agg | 존재 시 dump, 없으면 신규 CREATE | Phase 2 Task 2-0에서 확인 |
| account_health | 신규 생성 | |
| listings | WHERE business_model='dropship' | |
| sales | 신규 생성 (Phase 1+) | |

> **최종 6개 테이블.** amazon_search_results/agg가 control_tower.db에 없으면 schema_ds.sql에서 신규 생성. 어느 쪽이든 결과는 6개.

### purchase.db (19개 테이블)
(v4와 동일)

## EC2 리소스 확인 (Phase 1 시작 전 필수)
(v4와 동일)

## 작업 진행 순서

스킬 설치 → Phase 1 → Phase 2 → Phase 3 → Phase 4

## 참조 문서

```
docs/
├── purchase_agent_ia_spec_v1.1.md          ← 구매대행 IA 설계서
├── dropshipping_ia_spec_v1.0.md            ← 드랍쉬핑 IA 설계서
├── dropshipping_dashboard_spec_v1.0.md     ← 드랍쉬핑 대시보드 기획서
└── platform_architecture_v1.0.md           ← 플랫폼 통합 아키텍처
```

> **파일명 규칙**: 영문 소문자 + 언더스코어. 버전은 파일명에 포함.
