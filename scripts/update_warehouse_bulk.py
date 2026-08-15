"""CJ 전체 상품 창고 확인 — 카테고리 순회, pid 매칭."""
import os, sys, time, requests, sqlite3, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
envpath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
with open(envpath) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault("DS_DB_PATH", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backend", "dropshipping", "dropshipping.db"
))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("warehouse")

from backend.dropshipping.services.cj_service import _get_token, get_category_tree, EXCLUDED_LEVEL1

CJ_API_BASE = "https://developers.cjdropshipping.com/api2.0/v1"


def main():
    token = _get_token()
    if not token:
        logger.error("Token failed")
        return

    conn = sqlite3.connect(os.environ["DS_DB_PATH"])
    conn.row_factory = sqlite3.Row

    # 미확인 상품 pid 세트
    unknown_pids = {}
    rows = conn.execute(
        "SELECT id, external_id FROM collected_products "
        "WHERE warehouse_country=? AND us_warehouse=?",
        ("US", 0),
    ).fetchall()
    for r in rows:
        if r["external_id"]:
            unknown_pids[r["external_id"]] = r["id"]

    logger.info("Unknown warehouse: %d products", len(unknown_pids))

    categories = get_category_tree()
    categories = [c for c in categories if c["level1"] not in EXCLUDED_LEVEL1]

    api_calls = 0
    updated = 0
    max_calls = 900

    for idx, cat in enumerate(categories):
        if api_calls >= max_calls:
            logger.info("API limit reached (%d calls)", api_calls)
            break

        cat_id = cat["id"]

        for page in range(1, 21):
            if api_calls >= max_calls:
                break

            time.sleep(1)
            api_calls += 1

            try:
                resp = requests.get(
                    CJ_API_BASE + "/product/list",
                    headers={"CJ-Access-Token": token},
                    params={"categoryId": cat_id, "pageNum": page, "pageSize": 50},
                    timeout=20,
                )
                data = resp.json()
                if not data.get("result"):
                    msg = data.get("message", "")
                    if "Too Many" in msg:
                        logger.warning("RATE LIMITED at call %d", api_calls)
                        api_calls = max_calls
                    break

                items = (data.get("data") or {}).get("list") or []
                if not items:
                    break

                for item in items:
                    pid = item.get("pid", "")
                    if pid not in unknown_pids:
                        continue

                    codes = item.get("shippingCountryCodes") or []
                    if not codes:
                        continue

                    us_wh = 1 if "US" in codes else 0
                    if "US" in codes:
                        wh_country = "US"
                    elif "CN" in codes:
                        wh_country = "CN"
                    else:
                        wh_country = str(codes[0])

                    db_id = unknown_pids[pid]
                    conn.execute(
                        "UPDATE collected_products SET us_warehouse=?, warehouse_country=? WHERE id=?",
                        (us_wh, wh_country, db_id),
                    )
                    updated += 1
                    del unknown_pids[pid]

                if len(items) < 50:
                    break

            except Exception as e:
                logger.error("API error: %s", str(e)[:80])
                break

        if (idx + 1) % 20 == 0:
            conn.commit()
            logger.info(
                "Cat %d/%d, calls=%d, updated=%d, remaining=%d",
                idx + 1, len(categories), api_calls, updated, len(unknown_pids),
            )

    conn.commit()

    logger.info("=== DONE ===")
    logger.info("API calls: %d", api_calls)
    logger.info("Updated: %d", updated)
    logger.info("Still unknown: %d", len(unknown_pids))

    rows = conn.execute(
        "SELECT warehouse_country, us_warehouse, COUNT(*) cnt "
        "FROM collected_products GROUP BY warehouse_country, us_warehouse ORDER BY cnt DESC"
    ).fetchall()
    logger.info("=== Final Distribution ===")
    for r in rows:
        logger.info("  wh=%s, us_wh=%s: %s", repr(r[0]), r[1], r[2])

    conn.close()


if __name__ == "__main__":
    main()
