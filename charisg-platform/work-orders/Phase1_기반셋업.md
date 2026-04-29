# Phase 1: 기반 셋업 (v3)

> **목표**: 프로젝트 구조 + 공용 패키지(utils/migrations 포함) + Nginx + Hub API + Shell App
> **선행 조건**: EC2 사양 확인 + frontend-design 스킬 설치
> **완료 기준**: 로그인 → Hub 대시보드 표시

---

## Task 1-0: 사전 확인

(v2와 동일 — EC2 사양, 도메인, 스킬 설치)

---

## Task 1-1: 프로젝트 구조 생성

```bash
mkdir -p charisg-platform/{packages/{ui/{components,styles},auth,backend-shared/{ai,utils,migrations}},apps/{hub/src/{pages,api},dropshipping/src/{pages,components,stores,api},purchase/src/{pages,components,stores,api}},backend/{hub/{routers,migrations},dropshipping/{routers,services,migrations},purchase/{routers,services,migrations}},nginx,docs}
```

---

## Task 1-2: 백엔드 공용 패키지 (packages/backend-shared/)

**추출 대상 (~/dropship-crawler/backend/services/ 에서):**

| 파일/폴더 | 대상 경로 | 설명 |
|----------|----------|------|
| ai/ (폴더 전체) | backend-shared/ai/ | Gemini API |
| amazon.py | backend-shared/amazon.py | 크롤러 엔진 |
| base.py | backend-shared/base.py | 기반 클래스 |
| **utils/proxy_pool.py** | **backend-shared/utils/proxy_pool.py** | **프록시 풀 ★ 추가** |
| **utils/rate_limiter.py** | **backend-shared/utils/rate_limiter.py** | **요청 속도 제한 ★ 추가** |
| pricing_service.py | backend-shared/pricing_service.py | 가격/환율 |
| detail_page_service.py | backend-shared/detail_page_service.py | 상세페이지 |
| category_service.py | backend-shared/category_service.py | 카테고리 매핑 |
| process.py | backend-shared/process.py | 벌크 처리 |
| **migrations/__init__.py** | **backend-shared/migrations/__init__.py** | **마이그레이션 러너 로직 ★ 추가** |

**setup.py:**
```python
from setuptools import setup, find_packages
setup(
    name="charisg-backend-shared",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        "google-generativeai",
        "playwright",
        "httpx",
    ],
)
```

**database.py는 포함하지 않음.** DB 파일 경로가 API마다 다르므로 각 API에 복사하여 경로만 변경.

**마이그레이션 전략:**
- `backend-shared/migrations/__init__.py` — 마이그레이션 러너 공용 로직 (스키마 적용, 버전 추적)
- 각 API의 `migrations/schema_XX.sql` — 해당 DB 전용 스키마
- 초기 데이터는 control_tower.db에서 dump 후 import
- 이후 각 DB는 자체 마이그레이션 러너로 독립 운영

**검증:**
```bash
cd packages/backend-shared
pip install -e .
python -c "from backend_shared.utils.proxy_pool import ProxyPool; print('OK')"
python -c "from backend_shared.utils.rate_limiter import RateLimiter; print('OK')"
python -c "from backend_shared.migrations import MigrationRunner; print('OK')"
```

---

## Task 1-3 ~ 1-7

(v2와 동일 — 디자인 시스템, 인증 모듈, Hub API, Shell App, Nginx)

---

## Phase 1 완료 체크리스트

- [ ] EC2 사양 t3.medium 이상 확인
- [ ] frontend-design 스킬 설치 + 활성화 확인
- [ ] packages/backend-shared/ 추출 완료 — **utils/, migrations/ 포함 확인**
- [ ] pip install -e 동작 + proxy_pool, rate_limiter, MigrationRunner import 확인
- [ ] packages/ui/ 8개 공유 컴포넌트 구현
- [ ] packages/auth/ 인증 모듈 구현
- [ ] Hub API (port 8000) + 로그인/요약 동작
- [ ] Shell App (port 3000) + 로그인 → Hub 대시보드 동작
- [ ] Nginx 설정 + 문법 검사 통과
