"""smartstore_bulk_delete.py — 스마트스토어 카테고리별 일괄 삭제."""
import logging, os, sqlite3, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.purchase.services.naver_commerce_service import delete_product

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'backend', 'purchase', 'purchase.db')

CATEGORY_FILTERS = [
    '가구/인테리어>인테리어소품%',
    '가구/인테리어>수납가구%',
    '가구/인테리어>홈데코%',
    '가구/인테리어>침실가구%',
    '가구/인테리어>거실가구%',
    '가구/인테리어>주방가구%',
    '생활/건강>수납/정리용품%',
]

def get_targets():
    like_clauses = ' OR '.join(f"nc.whole_name LIKE '{f}'" for f in CATEGORY_FILTERS)
    sql = f"""
        SELECT l.id as listing_id, l.product_id, l.channel_product_id,
               COALESCE(nc.whole_name, p.category_path) as category
        FROM listings_pa l
        JOIN products p ON l.product_id = p.id
        LEFT JOIN naver_categories nc ON p.category_path = nc.id
        WHERE l.channel = 'smartstore' AND l.status = 'listed'
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
    logger.info(f'삭제 대상: {len(targets)}건')
    if not targets:
        return

    conn = sqlite3.connect(DB_PATH)
    success = fail = 0
    t0 = time.time()

    for i, t in enumerate(targets, 1):
        product_no = str(t['channel_product_id'])
        ok, err = delete_product(product_no)

        if ok:
            conn.execute('UPDATE listings_pa SET status = %s WHERE id = ?' % repr('archived'), (t['listing_id'],))
            success += 1
        else:
            logger.warning(f'삭제 실패: product_no={product_no} err={err}')
            fail += 1

        if i % 50 == 0:
            conn.commit()
            elapsed = time.time() - t0
            rate = i / elapsed * 60
            logger.info(f'진행: {i}/{len(targets)} 성공={success} 실패={fail} ({rate:.0f}/min)')

    conn.commit()
    conn.close()
    elapsed = time.time() - t0
    logger.info(f'완료: {len(targets)}건 — 성공={success} 실패={fail} ({elapsed:.0f}초)')

if __name__ == '__main__':
    main()
