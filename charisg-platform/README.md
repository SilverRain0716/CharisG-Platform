# Charis G Platform

> Dropshipping (Amazon US FBM) + Purchase Agent (Amazon US → Korea) 를 하나의 도메인에서 운영하는 마이크로 프론트엔드(B-3) 플랫폼.

## 구조

```
charisg-platform/
├── packages/
│   ├── backend-shared/        # 공용 백엔드 모듈 (ai, utils, migrations 등)
│   ├── ui/                    # 공유 디자인 시스템 (8 컴포넌트)
│   └── auth/                  # JWT httpOnly 쿠키 공유
├── apps/
│   ├── hub/                   # Shell App        port 3000
│   ├── dropshipping/          # DS App (8 뷰)    port 3001
│   └── purchase/              # PA App (7 메뉴)  port 3002
├── backend/
│   ├── hub/                   # Hub API + hub.db (3 테이블)        port 8000
│   ├── dropshipping/          # DS API + dropshipping.db (6 테이블) port 8001
│   └── purchase/              # PA API + purchase.db (19 테이블)    port 8002
├── nginx/                     # 리버스 프록시 설정
├── scripts/                   # selftest 등
├── ecosystem.config.js        # PM2 6 프로세스
├── requirements.txt           # 백엔드 의존성
└── pnpm-workspace.yaml
```

## 개발 환경 셋업

```bash
# 1. 백엔드
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e packages/backend-shared

# 2. 프론트엔드
pnpm install

# 3. .env
cp .env.example .env
# 필수 키: JWT_SECRET, CTRL_ADMIN_PASS, GEMINI_API_KEY 등 채우기
```

## 로컬 실행 (6 프로세스 동시 기동)

각 터미널에서 1개씩, 또는 PM2 로 한 번에:

```bash
# PM2 (권장)
pm2 start ecosystem.config.js

# 수동 (개발 모드)
PYTHONPATH=$(pwd) CHARISG_ROOT=$(pwd) uvicorn backend.hub.main:app          --port 8000 --reload
PYTHONPATH=$(pwd) CHARISG_ROOT=$(pwd) uvicorn backend.dropshipping.main:app --port 8001 --reload
PYTHONPATH=$(pwd) CHARISG_ROOT=$(pwd) uvicorn backend.purchase.main:app     --port 8002 --reload
pnpm dev:shell       # 3000
pnpm dev:ds          # 3001
pnpm dev:pa          # 3002
```

접속: <http://localhost:3000> → 로그인 → Hub 대시보드.

## 정적 자체 검증

```bash
python3 scripts/selftest_static.py
```

검증 항목:

1. 모든 `.py` AST 파싱
2. 3개 DB schema 적용 (in-memory sqlite)
3. 모든 라우터 `router` 심볼 export
4. main.py 라우터 import 정합
5. 프론트 package.json + 페이지 카운트

(venv/pip 없이 stdlib 만으로 동작)

## EC2 배포 의존 항목 (현재 미해결)

다음 작업은 EC2 가 필요합니다 (현재 비용 절감 목적으로 정지 상태):

| 항목 | 내용 |
|---|---|
| Task 2-0 | `~/dropship-crawler/control_tower.db` 의 `amazon_search_results/agg` 테이블 실존 확인 |
| 데이터 마이그레이션 | `business_model` 컬럼 기준으로 `dropship`/`purchase_agent` 분리 → 3 DB 로 dump/import |
| 크롤러 마이그레이션 | `amazon_keyword_crawler.py` + cron + GitHub Actions 경로 변경 |
| 차단방지 검증 | Webshare 프록시 + 5–10 키워드 소규모 테스트 |
| Nginx + SSL | certbot --nginx |
| PM2 부팅 자동기동 | `pm2 startup` + `pm2 save` |
| 1 주 모니터링 | 메모리 / CPU / 에러율 |

## 작업 원칙

- **분리 원칙**: `apps/dropshipping/` 은 `apps/purchase/` 를 import 하지 않는다.
- **scoring/monitoring 은 공용이 아님**: DS(3축 + Hard Filter 8) ↔ PA(2축 + 통관 필터) 로직이 다르다.
- **database.py 는 각 API 전용**: 마이그레이션 러너 로직만 `backend_shared.migrations` 에 공유.
- **인증 공유**: Shell 로그인 → JWT httpOnly 쿠키 → 서브 앱 자동 공유.

## 참조 문서

- `../docs/platform_architecture_v1.0.md`
- `../docs/dropshipping_dashboard_spec_v1.0.md`
- `../docs/dropshipping_ia_spec_v1.0.md`
- `../docs/purchase_agent_ia_spec_v1.1.md`
- `../work-orders/Phase1_기반셋업.md` ~ `Phase4_모노리스_폐기.md`
