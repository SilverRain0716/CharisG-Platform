"""draft 잔여물 자동 청소 — 부모 그룹이 종결됐고 활성 큐가 없는 draft 자식을 archived.

종결(terminal) = group_registration_queue.status IN (done, skipped, done_singles, error)
활성(active)   = status IN (queued, registering, pre_scanning, on_hold)
→ 부모가 terminal 이고 active 큐가 전혀 없는 draft 자식만 archive.
  (active 가 하나라도 있으면 보존 — 재처리 가능성)
단독(parent 없음) 및 active 부모 자식은 건드리지 않음.

재발 대비 idempotent: 매번 같은 기준으로 잔여물만 쓸어담음. 데일리 타이머로 가동.
--apply 없으면 집계만.
"""
import os, sqlite3, argparse

DB = os.path.expanduser("~/CharisG-Platform/charisg-platform/backend/purchase/purchase.db")
ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); a = ap.parse_args()

conn = sqlite3.connect(DB, timeout=180)
conn.execute("PRAGMA busy_timeout=180000")

TERMINAL = "('done','skipped','done_singles','error')"
ACTIVE = "('queued','registering','pre_scanning','on_hold')"

WHERE = (
    "status='draft' AND parent_asin IS NOT NULL AND parent_asin<>'' "
    f"AND parent_asin IN (SELECT parent_asin FROM group_registration_queue WHERE status IN {TERMINAL}) "
    f"AND parent_asin NOT IN (SELECT parent_asin FROM group_registration_queue WHERE status IN {ACTIVE})"
)

n = conn.execute(f"SELECT COUNT(*) FROM products WHERE {WHERE}").fetchone()[0]
print(f"청소대상(부모 terminal & active없음) draft 자식: {n}")
# 보존 확인
keep_active = conn.execute(
    f"SELECT COUNT(*) FROM products WHERE status='draft' AND parent_asin IN "
    f"(SELECT parent_asin FROM group_registration_queue WHERE status IN {ACTIVE})").fetchone()[0]
keep_solo = conn.execute(
    "SELECT COUNT(*) FROM products WHERE status='draft' AND (parent_asin IS NULL OR parent_asin='')").fetchone()[0]
print(f"보존: 활성부모 자식 {keep_active} + 단독 {keep_solo}")

if not a.apply:
    print("[DRY] --apply 로 실제 archive.")
    raise SystemExit(0)

c = conn.execute(f"UPDATE products SET status='archived' WHERE {WHERE}").rowcount
conn.commit()
remain = conn.execute("SELECT COUNT(*) FROM products WHERE status='draft'").fetchone()[0]
print(f"archived {c} → 남은 draft {remain}")
