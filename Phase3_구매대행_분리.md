# Phase 3: 구매대행 분리 (v3)

> **목표**: PA API + purchase.db + PA App 독립 구현
> **선행 조건**: Phase 2 완료
> **완료 기준**: /purchase 접속 시 7개 메뉴 동작
> **참조**: `docs/구매대행_IA_설계서_v1.1.md`

---

## Task 3-1: PA API 분리 (backend/purchase/)

**PA 전용 서비스 추출 (→ backend/purchase/services/):**
| 파일 | 설명 |
|------|------|
| naver_datalab_service.py | 네이버 데이터랩 크롤링 |
| keyword_cluster_service.py | AI 키워드 클러스터링 |
| naver_searchad_service.py | 네이버 검색광고 API |
| margin_calculator.py | 마진 계산 |
| customs_service.py | 통관 리스크 체크 |
| competition_service.py | 경쟁 가격 비교 |
| tracking_service.py | 배송추적 (구현 완료, CJ 자동 + 수동) |
| order_receiver_service.py | 주문 수신 |
| tariff_service.py | 관세 자동화 (구현 완료, AI HS코드 + 12,469건) |
| scoring_service.py (PA 부분) | PA용 스코어링 (2축 + 통관 필터) |
| **naver_commerce_service.py** | **네이버 커머스 API 클라이언트 ★ 추가** |
| **smartstore_lister.py** | **스마트스토어 리스팅 모듈 ★ 추가** |
| **coupang_service.py** | **쿠팡 업로드 모듈 ★ 추가** |
| **stock_monitor_service.py** | **재고 모니터링 (네이버/쿠팡 리스팅 비활성화) ★ 추가** |
| **price_monitor.py** | **가격 모니터링 (아마존 가격→마진 재계산) ★ 추가** |

**총 15개 PA 전용 서비스.**

**공용 모듈 참조 (packages/backend-shared):**
ai/, amazon.py, base.py, utils/, pricing_service.py, detail_page_service.py, category_service.py, process.py, migrations/

**PA 전용 database.py + 마이그레이션:**
```
backend/purchase/
├── database.py                 ← purchase.db 경로
└── migrations/
    └── schema_pa.sql           ← PA 전용 스키마 (19개 테이블)
```

**라우터 매핑 (★ 실제 파일명 정확 매핑):**
| 기존 라우터 파일 | 신규 경로 | 설명 |
|----------------|----------|------|
| naver_datalab.py | /api/pa/datalab/* | 네이버 데이터랩 |
| search_ad.py | /api/pa/searchad/* | 검색광고 API |
| keywords.py | /api/pa/keywords/* | 키워드 + 클러스터링 |
| amazon.py (PA 부분) | /api/pa/sourcing/* | 아마존 소싱 크롤링 |
| pricing.py (마진 포함) | /api/pa/margin/* | 마진 + 고객 총 비용 |
| customs.py | /api/pa/customs/* | 통관 리스크 |
| competition.py | /api/pa/competition/* | 경쟁 가격 비교 |
| products.py (PA) | /api/pa/products/* | 상품 관리 |
| detail_page.py | /api/pa/detail-page/* | 13섹션 상세페이지 |
| smartstore.py | /api/pa/smartstore/* | 스마트스토어 API |
| coupang.py | /api/pa/coupang/* | 쿠팡 API |
| orders.py | /api/pa/orders/* | 6단계 주문 |
| tracking.py | /api/pa/tracking/* | 배송추적 |
| cs.py | /api/pa/cs/* | CS 티켓 |
| returns.py | /api/pa/returns/* | 반품·환불 |
| monitor.py (PA) | /api/pa/monitor/* | 모니터링 (stock_monitor + price_monitor 포함) |
| settings.py (PA) | /api/pa/settings/* | 설정 |

**DB 마이그레이션 (19개 테이블):**
```sql
-- 마이그레이션 러너 사용
python -c "
from backend_shared.migrations import MigrationRunner
runner = MigrationRunner('purchase.db')
runner.apply('migrations/schema_pa.sql')
"

-- 초기 데이터 import (business_model 기준 분리)
-- (v2와 동일한 쿼리)
```

---

## Task 3-2: PA App 구현

(v2와 동일 — 7개 메뉴, 배송추적 완료, 관세 완료 상태 반영)

---

## Task 3-3: Hub API summary 연결

(v2와 동일)

---

## Phase 3 완료 체크리스트

- [ ] PA 전용 서비스 **15개** 전부 backend/purchase/services/에 배치
- [ ] naver_commerce_service.py, smartstore_lister.py, coupang_service.py 포함 확인 ★
- [ ] stock_monitor_service.py, price_monitor.py PA 전용 배치 확인 ★
- [ ] PA 전용 database.py + migrations/schema_pa.sql 적용
- [ ] purchase.db 19개 테이블 생성 + 데이터 정합성 확인
- [ ] PA API 실행 (port 8002) + 전체 엔드포인트 동작
- [ ] PA App 7개 메뉴 전체 동작
- [ ] 배송추적: CJ 자동 + 수동 입력 동작 확인
- [ ] 관세: AI HS코드 + 12,469건 DB 조회 동작 확인
- [ ] 모니터링: stock_monitor + price_monitor 동작 확인 ★
- [ ] 3개 앱 간 전환 완전 동작
- [ ] 인증 공유 3개 앱 확인
