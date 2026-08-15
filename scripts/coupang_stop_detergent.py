"""coupang_stop_detergent.py — 쿠팡 세제류 판매중지 처리."""
import logging, os, sqlite3, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.purchase.services.coupang_service import stop_sales

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'backend', 'purchase', 'purchase.db')
CATEGORY_KEYWORDS = ['세제', '세정', '클리너', '세탁', '표백', '섬유유연제', '방향제', '탈취']

def get_targets():
    like_clauses = ' OR '.join(f"nc.whole_name LIKE '%{kw}%'" for kw in CATEGORY_KEYWORDS)
    sql = f"""
        SELECT l.id as listing_id, l.product_id, l.channel_product_id,
               COALESCE(nc.whole_name, p.category_path) as category
        FROM listings_pa l
        JOIN products p ON l.product_id = p.id
        LEFT JOIN naver_categories nc ON p.category_path = nc.id
        WHERE l.channel = 'coupang' AND l.status = 'listed'
          AND ({like_clauses})
        ORDER BY category
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    conn.close()
    return rows

def main():
    targets = get_targets()
    logger.info(f'쿠팡 판매중지 대상: {len(targets)}건')
    if not targets:
        return

    conn = sqlite3.connect(DB_PATH)
    success = fail = 0

    for i, t in enumerate(targets, 1):
        product_no = str(t['channel_product_id'])
        ok, err = stop_sales(product_no)

        if ok:
            conn.execute('UPDATE listings_pa SET status = ? WHERE id = ?', ('archived', t['listing_id']))
            success += 1
        else:
            logger.warning(f'판매중지 실패: {product_no} err={err}')
            fail += 1

        if i % 10 == 0:
            conn.commit()
            logger.info(f'진행: {i}/{len(targets)} 성공={success} 실패={fail}')

    conn.commit()
    conn.close()
    logger.info(f'완료: {len(targets)}건 — 성공={success} 실패={fail}')

if __name__ == '__main__':
    main()
