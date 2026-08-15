"""bulk_delete_detergent.py — 세제/세정/방향/탈취 카테고리 양 채널 동시 삭제."""
import logging, os, sqlite3, sys, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.purchase.services.naver_commerce_service import delete_product as naver_delete
from backend.purchase.services.coupang_service import delete_product as coupang_delete

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s [%(threadName)s] %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'backend', 'purchase', 'purchase.db')

CATEGORY_KEYWORDS = ['세제', '세정', '클리너', '세탁', '표백', '섬유유연제', '방향제', '탈취']

def get_targets(channel):
    like_clauses = ' OR '.join(f"nc.whole_name LIKE '%{kw}%'" for kw in CATEGORY_KEYWORDS)
    sql = f"""
        SELECT l.id as listing_id, l.product_id, l.channel_product_id,
               COALESCE(nc.whole_name, p.category_path) as category
        FROM listings_pa l
        JOIN products p ON l.product_id = p.id
        LEFT JOIN naver_categories nc ON p.category_path = nc.id
        WHERE l.channel = ? AND l.status = 'listed'
          AND ({like_clauses})
        ORDER BY category
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, (channel,)).fetchall()]
    conn.close()
    return rows

def run_channel(channel, delete_fn):
    targets = get_targets(channel)
    logger.info(f'{channel} 삭제 대상: {len(targets)}건')
    if not targets:
        return

    conn = sqlite3.connect(DB_PATH)
    success = fail = 0
    t0 = time.time()

    for i, t in enumerate(targets, 1):
        product_no = str(t['channel_product_id'])
        if channel == 'smartstore':
            ok, err = delete_fn(product_no)
        else:
            ok, err = delete_fn(product_no)

        if ok:
            conn.execute('UPDATE listings_pa SET status = ? WHERE id = ?', ('archived', t['listing_id']))
            success += 1
        else:
            logger.warning(f'{channel} 삭제 실패: {product_no} err={err}')
            fail += 1

        if i % 20 == 0:
            conn.commit()
            elapsed = time.time() - t0
            rate = i / elapsed * 60 if elapsed > 0 else 0
            logger.info(f'{channel} 진행: {i}/{len(targets)} 성공={success} 실패={fail} ({rate:.0f}/min)')

    conn.commit()
    conn.close()
    elapsed = time.time() - t0
    logger.info(f'{channel} 완료: {len(targets)}건 — 성공={success} 실패={fail} ({elapsed:.0f}초)')

def main():
    t_ss = threading.Thread(target=run_channel, args=('smartstore', naver_delete), name='smartstore')
    t_cp = threading.Thread(target=run_channel, args=('coupang', coupang_delete), name='coupang')

    t_ss.start()
    t_cp.start()

    t_ss.join()
    t_cp.join()
    logger.info('양 채널 삭제 완료')

if __name__ == '__main__':
    main()
