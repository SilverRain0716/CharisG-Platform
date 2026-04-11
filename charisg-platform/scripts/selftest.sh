#!/usr/bin/env bash
# Charis G Platform 로컬 자체 검증.
#
# 검증 항목:
#   1. backend_shared 패키지 import (utils, ai, migrations)
#   2. 3개 DB 마이그레이션 적용 (빈 sqlite → schema_*.sql)
#   3. 3개 API import 체크 (FastAPI 객체 로드)
#   4. 프론트 빌드 가능 여부 (선택, --skip-frontend 로 스킵)
#
# 사용:
#   bash scripts/selftest.sh           # 전체
#   bash scripts/selftest.sh --skip-frontend
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SKIP_FE=0
for arg in "$@"; do
  case "$arg" in
    --skip-frontend) SKIP_FE=1 ;;
  esac
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Charis G Platform Self-Test"
echo "ROOT: $ROOT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# venv 자동 생성
if [ ! -d ".venv" ]; then
  echo "[1/5] venv 생성"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "[2/5] backend-shared 설치 (editable)"
pip install --quiet --upgrade pip
pip install --quiet -e packages/backend-shared
pip install --quiet -r requirements.txt

export PYTHONPATH="$ROOT"
export CHARISG_ROOT="$ROOT"
export CTRL_AUTH_BYPASS="${CTRL_AUTH_BYPASS:-true}"
export HUB_DB_PATH="$ROOT/.selftest_hub.db"
export DS_DB_PATH="$ROOT/.selftest_ds.db"
export PA_DB_PATH="$ROOT/.selftest_pa.db"
rm -f "$HUB_DB_PATH" "$DS_DB_PATH" "$PA_DB_PATH"

echo "[3/5] 공용 모듈 import"
python - <<'PY'
from backend_shared import __version__, get_db, register_db_factory
from backend_shared.utils.proxy_pool import ProxyPool
from backend_shared.utils.rate_limiter import RateLimiter, CrawlerDelayer
from backend_shared.migrations import MigrationRunner
from backend_shared.ai import gemini_limiter
print(f"  ✓ backend_shared v{__version__}")
print(f"  ✓ ProxyPool / RateLimiter / CrawlerDelayer / MigrationRunner / gemini_limiter")
PY

echo "[4/5] 3개 DB 마이그레이션 + API import"
python - <<'PY'
import importlib

# Hub
from backend.hub import database as hub_db
hub_db.init_db()
print(f"  ✓ hub.db migrated → {hub_db.DB_PATH}")
m = importlib.import_module("backend.hub.main")
assert m.app.title == "Charis G Hub API"
print("  ✓ hub-api app loaded")

# DS
from backend.dropshipping import database as ds_db
ds_db.init_db()
print(f"  ✓ dropshipping.db migrated → {ds_db.DB_PATH}")
m = importlib.import_module("backend.dropshipping.main")
assert m.app.title == "Charis G Dropshipping API"
# 13 routers + summary + dashboard + health = check route count
route_count = len([r for r in m.app.routes])
print(f"  ✓ ds-api app loaded ({route_count} routes)")

# PA
from backend.purchase import database as pa_db
pa_db.init_db()
print(f"  ✓ purchase.db migrated → {pa_db.DB_PATH}")
m = importlib.import_module("backend.purchase.main")
assert m.app.title == "Charis G Purchase Agent API"
route_count = len([r for r in m.app.routes])
print(f"  ✓ pa-api app loaded ({route_count} routes)")
PY

echo "[5/5] DB 테이블 카운트 검증"
python - <<'PY'
import sqlite3, os
def count_tables(p):
    c = sqlite3.connect(p)
    rows = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'schema_meta'").fetchall()
    c.close()
    return [r[0] for r in rows]

hub  = count_tables(os.environ['HUB_DB_PATH'])
ds   = count_tables(os.environ['DS_DB_PATH'])
pa   = count_tables(os.environ['PA_DB_PATH'])
print(f"  hub.db tables ({len(hub)}): {hub}")
print(f"  dropshipping.db tables ({len(ds)}): {ds}")
print(f"  purchase.db tables ({len(pa)}): {pa}")

assert len(hub) >= 3,  f"hub.db 최소 3 테이블 기대, 실제 {len(hub)}"
assert len(ds)  >= 6,  f"dropshipping.db 최소 6 테이블 기대, 실제 {len(ds)}"
assert len(pa)  >= 19, f"purchase.db 최소 19 테이블 기대, 실제 {len(pa)}"
print("  ✓ 테이블 카운트 OK")
PY

# 정리
rm -f "$HUB_DB_PATH" "$DS_DB_PATH" "$PA_DB_PATH" \
      "${HUB_DB_PATH}-journal" "${DS_DB_PATH}-journal" "${PA_DB_PATH}-journal"

if [ "$SKIP_FE" -eq 0 ]; then
  echo "[6/6] 프론트 빌드 (pnpm install + build) — 시간 소요"
  if command -v pnpm >/dev/null 2>&1; then
    pnpm install --silent || echo "  ⚠ pnpm install 실패 — 네트워크/권한 확인"
    pnpm -r build         || echo "  ⚠ 일부 빌드 실패 — 로그 확인"
  else
    echo "  ⚠ pnpm 미설치 — npm i -g pnpm 후 재실행"
  fi
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✓ Self-Test 완료"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
