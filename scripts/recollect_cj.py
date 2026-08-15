"""CJ 전수 재수집 — shippingCountryCodes 기반 창고 구분."""
import os, sys, time, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault("DS_DB_PATH", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backend/dropshipping/dropshipping.db"
))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("recollect")

from backend.dropshipping.services.cj_service import collect_full_catalog

def progress(phase, current, total, message):
    logger.info(f"[{phase}] {current}/{total} — {message}")

logger.info("=" * 60)
logger.info("CJ 전수 재수집 시작 (shippingCountryCodes 파싱 적용)")
logger.info("=" * 60)

start = time.time()
result = collect_full_catalog(
    progress_cb=progress,
    max_pages_per_category=20,  # 카테고리당 최대 20페이지 (1000개)
    page_size=50,
    skip_excluded=True,
)
elapsed = time.time() - start

logger.info("=" * 60)
logger.info(f"완료: {elapsed/60:.1f}분 소요")
logger.info(f"  카테고리: {result[categories]}")
logger.info(f"  API 조회: {result[raw_collected]}")
logger.info(f"  필터 통과: {result[filter_passed]}")
logger.info(f"  신규 저장: {result[saved]}")
logger.info("=" * 60)

# 재수집 후 창고 분포 확인
import sqlite3
conn = sqlite3.connect(os.environ["DS_DB_PATH"])
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT warehouse_country, us_warehouse, hard_filter_pass,
           COUNT(*) cnt
    FROM collected_products WHERE source=cj
    GROUP BY warehouse_country, us_warehouse, hard_filter_pass
    ORDER BY cnt DESC
""").fetchall()
logger.info("=== 창고 분포 (재수집 후) ===")
for r in rows:
    logger.info(f"  wh_country={r[0]!r}, us_wh={r[1]}, filter_pass={r[2]}, count={r[3]}")
conn.close()
