"""migrate_split_orders.py — orders 그룹을 cold.db → hot.db 로 이주.

실행 절차:
  1. service stop
  2. python -m backend.purchase.scripts.migrate_split_orders
  3. (검증 OK 시) cold.db 의 orders/order_steps/cs_tickets/returns_pa 테이블을
     _archive_orders / _archive_order_steps / ... 로 rename (롤백 가능)
  4. service start

idempotent: 이미 hot.db 에 데이터 있으면 skip (count 비교).

Dry-run 모드:
  python -m backend.purchase.scripts.migrate_split_orders --dry-run
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend.purchase.database import DB_PATH, DB_PATH_HOT, init_db


TABLES_TO_MIGRATE = ["orders", "order_steps", "cs_tickets", "returns_pa"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open(path: Path, attach_cold: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    if attach_cold:
        conn.execute(f"ATTACH DATABASE '{DB_PATH}' AS cold")
    return conn


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()
        return row["c"] if row else 0
    except sqlite3.OperationalError:
        return -1  # table 없음


def get_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in rows]


def migrate_orders_with_denormalize(dry_run: bool = False) -> dict:
    """orders 이주 + denormalized 컬럼 채움 (products join)."""
    print(f"\n=== orders 이주 ===")

    cold = _open(DB_PATH)
    hot = _open(DB_PATH_HOT, attach_cold=False)

    src_count = count_rows(cold, "orders")
    dst_count = count_rows(hot, "orders")
    print(f"cold.orders: {src_count}건 / hot.orders: {dst_count}건")

    if dst_count > 0:
        print("⚠️ hot.orders 가 이미 데이터 있음 — skip")
        cold.close()
        hot.close()
        return {"skipped": True, "src_count": src_count, "dst_count": dst_count}

    if src_count <= 0:
        print("cold.orders 가 비어있음 — skip")
        cold.close()
        hot.close()
        return {"src_count": 0, "dst_count": 0, "migrated": 0}

    # cold.orders 컬럼 + cold.products join 으로 denormalized 채움
    cold_cols = get_columns(cold, "orders")
    hot_cols = get_columns(hot, "orders")
    common_cols = [c for c in cold_cols if c in hot_cols]
    print(f"공통 컬럼: {len(common_cols)}개 — {common_cols}")

    # SELECT cold.orders + cold.products denormalized 정보
    rows = cold.execute(f"""
        SELECT o.*,
               p.title_ko AS p_title_ko,
               p.title_en AS p_title_en,
               p.brand AS p_brand,
               p.images_json AS p_images_json,
               p.asin AS p_asin
        FROM orders o
        LEFT JOIN products p ON p.id = o.product_id
    """).fetchall()
    print(f"이주 대상: {len(rows)}건")

    if dry_run:
        print("--- DRY RUN — INSERT 안 함 ---")
        for r in rows[:3]:
            print(f"  sample: id={r['id']} channel={r['channel']} title={r['p_title_ko'] or r['p_title_en']}")
        cold.close()
        hot.close()
        return {"dry_run": True, "would_migrate": len(rows)}

    # INSERT into hot.orders
    insert_cols = common_cols + [
        "product_name_cache", "product_image_cache", "brand_cache", "asin_cache"
    ]
    placeholders = ",".join("?" * len(insert_cols))
    sql = f"INSERT INTO orders ({','.join(insert_cols)}) VALUES ({placeholders})"

    inserted = 0
    errors = 0
    for r in rows:
        # denormalized cache
        title = r["p_title_ko"] or r["p_title_en"] or ""
        first_img = ""
        try:
            imgs = json.loads(r["p_images_json"] or "[]")
            if imgs:
                first_img = imgs[0] if isinstance(imgs[0], str) else (imgs[0].get("url") or "")
        except Exception:
            pass

        values = [r[c] for c in common_cols] + [
            title[:200] if title else None,
            first_img[:500] if first_img else None,
            r["p_brand"],
            r["p_asin"],
        ]

        try:
            hot.execute(sql, values)
            inserted += 1
        except Exception as e:
            errors += 1
            print(f"  ❌ id={r['id']} INSERT 실패: {str(e)[:100]}")

    hot.commit()

    final_dst = count_rows(hot, "orders")
    print(f"✅ orders 완료 — 이주 {inserted}/{src_count}, 에러 {errors}, hot 최종 {final_dst}")

    cold.close()
    hot.close()
    return {"migrated": inserted, "errors": errors, "src_count": src_count, "dst_count": final_dst}


def migrate_simple_table(table: str, dry_run: bool = False) -> dict:
    """단순 테이블 이주 (denormalized 없음): order_steps, cs_tickets, returns_pa."""
    print(f"\n=== {table} 이주 ===")

    cold = _open(DB_PATH)
    hot = _open(DB_PATH_HOT)

    src_count = count_rows(cold, table)
    dst_count = count_rows(hot, table)
    print(f"cold.{table}: {src_count}건 / hot.{table}: {dst_count}건")

    if dst_count > 0:
        print(f"⚠️ hot.{table} 이미 데이터 있음 — skip")
        cold.close()
        hot.close()
        return {"skipped": True}

    if src_count <= 0:
        print(f"cold.{table} 비어있음 — skip")
        cold.close()
        hot.close()
        return {"src_count": 0, "migrated": 0}

    cold_cols = get_columns(cold, table)
    hot_cols = get_columns(hot, table)
    common_cols = [c for c in cold_cols if c in hot_cols]

    rows = cold.execute(f"SELECT {','.join(common_cols)} FROM {table}").fetchall()
    print(f"이주 대상: {len(rows)}건")

    if dry_run:
        print("--- DRY RUN ---")
        cold.close()
        hot.close()
        return {"dry_run": True, "would_migrate": len(rows)}

    placeholders = ",".join("?" * len(common_cols))
    sql = f"INSERT INTO {table} ({','.join(common_cols)}) VALUES ({placeholders})"

    inserted = 0
    for r in rows:
        try:
            hot.execute(sql, [r[c] for c in common_cols])
            inserted += 1
        except Exception as e:
            print(f"  ❌ id={r['id'] if 'id' in r.keys() else '?'} 실패: {str(e)[:100]}")
    hot.commit()

    final_dst = count_rows(hot, table)
    print(f"✅ {table} 완료 — 이주 {inserted}/{src_count}, hot 최종 {final_dst}")

    cold.close()
    hot.close()
    return {"migrated": inserted, "src_count": src_count, "dst_count": final_dst}


def archive_cold_tables() -> None:
    """이주 후 cold.db 의 orders/order_steps/cs_tickets/returns_pa 를 _archive_* 로 rename.

    완전 삭제 안 함 — 롤백 가능하게 보존.
    """
    print(f"\n=== cold.db 의 orders 그룹 archive ===")
    cold = _open(DB_PATH)
    ts = _now().replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    for table in TABLES_TO_MIGRATE:
        archive_name = f"_archive_{table}_{ts}"
        try:
            cold.execute(f"ALTER TABLE {table} RENAME TO {archive_name}")
            print(f"  ✅ {table} → {archive_name}")
        except sqlite3.OperationalError as e:
            print(f"  ⚠️ {table} archive 실패: {e}")
    cold.commit()
    cold.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="이주 시뮬레이션만 (INSERT 안 함)")
    parser.add_argument("--archive", action="store_true",
                        help="이주 완료 후 cold.db 의 테이블을 _archive_* 로 rename")
    args = parser.parse_args()

    print(f"DB_PATH (cold): {DB_PATH}")
    print(f"DB_PATH_HOT (hot): {DB_PATH_HOT}")
    print(f"dry-run: {args.dry_run}")

    # 마이그레이션 적용 (hot.db 신규 생성 포함)
    if not args.dry_run:
        init_db()

    results = {}
    results["orders"] = migrate_orders_with_denormalize(args.dry_run)
    for tbl in ["order_steps", "cs_tickets", "returns_pa"]:
        results[tbl] = migrate_simple_table(tbl, args.dry_run)

    print("\n=== 검증 (cold vs hot count 일치) ===")
    cold = _open(DB_PATH)
    hot = _open(DB_PATH_HOT)
    all_match = True
    for tbl in TABLES_TO_MIGRATE:
        c = count_rows(cold, tbl)
        h = count_rows(hot, tbl)
        ok = c == h or c <= 0
        all_match = all_match and ok
        mark = "✅" if ok else "❌"
        print(f"  {mark} {tbl}: cold={c} hot={h}")
    cold.close()
    hot.close()

    if not args.dry_run and args.archive and all_match:
        archive_cold_tables()
        print("\n✅ 마이그레이션 + archive 완료")
    elif not args.dry_run:
        print("\n✅ 마이그레이션 완료 (cold.db 테이블 archive 안 함 — --archive 옵션 사용)")

    return 0 if all_match else 1


if __name__ == "__main__":
    sys.exit(main())
