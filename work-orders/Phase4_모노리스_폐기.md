# Phase 4: 모노리스 폐기 (v3)

> **목표**: 기존 모노리스 아카이브 + 통합 테스트 + 프로덕션 전환
> **선행 조건**: Phase 3 완료 (3개 앱 모두 동작)
> **완료 기준**: 기존 모노리스 중지, B-3 구조만으로 운영

---

## Task 4-1: 통합 테스트

(v2와 동일 + 아래 추가)

추가 테스트 항목:
- [ ] PA 배송추적: CJ 자동추적 + 수동 입력 동작
- [ ] PA 관세: AI HS코드 → 관세청 DB 12,469건 → 세율 반환
- [ ] DS 계정 건강도: 수동 입력 → 게이지 표시
- [ ] DS Hard Filter: 8개 조건 전부 동작 + 탈락 사유별 분포 차트
- [ ] DS 크롤러: amazon_keyword_crawler 실행 → 차단 방지 설정 정상 동작
- [ ] 공용 모듈: DS/PA API 양쪽에서 backend-shared의 utils/, migrations/ 정상 import

---

## Task 4-2: 크롤러 마이그레이션 (★ 별도 Task)

### 4-2-1. amazon_keyword_crawler.py 마이그레이션

**의존성 전체 목록 (★ 보완):**

| 의존성 | 유형 | 마이그레이션 영향 |
|--------|------|----------------|
| Playwright (Chromium) | 런타임 | 설치 확인 필요 |
| Webshare Proxy (10 US IP) | 외부 서비스 | .env 프록시 인증정보 경로 변경 |
| utils/proxy_pool.py | 공용 모듈 | backend-shared에서 import 경로 변경 |
| utils/rate_limiter.py | 공용 모듈 | backend-shared에서 import 경로 변경 |
| collected_products 테이블 | DB | dropshipping.db에서 키워드 추출 |
| amazon_search_results 테이블 | DB | dropshipping.db에 결과 저장 |
| amazon_search_agg 테이블 | DB | dropshipping.db에 집계 저장 |
| scoring_service.py | DS 전용 | Gap Score 데이터 소비 |
| amazon_fee_service.py | DS 전용 | Referral Fee 참조 |
| .env | 설정 | 프록시 인증, DB 경로, API 키 |

**차단 방지 설정 유지 확인:**

```bash
# 크롤러 코드에서 차단 방지 관련 상수 검색
grep -n "delay\|sleep\|cooldown\|captcha\|user.agent\|proxy" \
  backend/dropshipping/services/amazon_keyword_crawler.py
```

- [ ] 15-25초 랜덤 딜레이 유지
- [ ] 50 키워드마다 2-3분 쿨다운 유지
- [ ] CAPTCHA 감지 시 5분 대기 유지
- [ ] 연속 실패 임계값 (3회→10분, 5회→중단) 유지
- [ ] User-Agent 로테이션 풀 유지
- [ ] Webshare 프록시 연동 정상

**경로 변경 확인:**

```bash
grep -rn "dropship-crawler\|control_tower.db\|from backend\|from services" \
  backend/dropshipping/services/amazon_keyword_crawler.py
```

| 변경 대상 | 기존 | 신규 |
|----------|------|------|
| import | from backend.services.xxx | from backend_shared.xxx 또는 DS services |
| DB 경로 | control_tower.db | dropshipping.db |
| .env 경로 | ~/dropship-crawler/.env | ~/charisg-platform/.env |

### 4-2-2. GitHub Actions 워크플로우 마이그레이션

(v2와 동일)

### 4-2-3. 환경변수 마이그레이션

(v2와 동일)

### 4-2-4. cron/systemd 스케줄 확인

(v2와 동일)

**검증:**
- 크롤러 수동 실행 → dropshipping.db에 데이터 저장 확인
- **차단 방지 설정이 정상 동작하는지 소규모 테스트 (5-10 키워드)**
- GitHub Actions 트리거 → 정상 완료

---

## Task 4-3: 프로세스 매니저 (PM2)

(v2와 동일 + EC2 리소스 모니터링)

---

## Task 4-4: Nginx 최종 설정 + SSL

(v2와 동일)

---

## Task 4-5: 기존 모노리스 아카이브

(v2와 동일)

---

## Task 4-6: 1주일 모니터링

(v2와 동일)

---

## Phase 4 완료 체크리스트

- [ ] 통합 테스트 전체 통과 (Hard Filter, 크롤러 차단방지 포함)
- [ ] 크롤러 마이그레이션 완료 (import, DB, .env, 프록시, 차단방지 전부)
- [ ] 크롤러 소규모 테스트 (5-10 키워드) 정상 동작
- [ ] PM2 6개 프로세스 online + 부팅 자동 시작
- [ ] EC2 리소스 안정 (메모리 80% 미만)
- [ ] Nginx + SSL 동작
- [ ] 기존 모노리스 아카이브 (삭제 아님)
- [ ] control_tower.db 백업 완료
- [ ] 1주일 모니터링 안정

---

## 전체 프로젝트 완료 기준

- {도메인}/ → Hub 대시보드 (로그인 후)
- {도메인}/dropshipping/* → DS App (**8개 뷰**, Performance는 placeholder)
- {도메인}/purchase/* → PA App (7개 메뉴)
- 3개 앱 간 자유로운 전환 (Global Top Bar + 드롭다운)
- 단일 로그인으로 전체 플랫폼 접근
- 기존 모노리스 완전 퇴역
- 크롤러 새 구조에서 정상 동작 (차단 방지 설정 유지)
