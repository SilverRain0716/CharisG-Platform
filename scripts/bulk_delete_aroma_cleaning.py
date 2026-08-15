"""bulk_delete_aroma_cleaning.py — 아로마/캔들/청소/세척 카테고리 양 채널 삭제."""
import logging, os, sqlite3, sys, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.purchase.services.naver_commerce_service import delete_product as naver_delete
from backend.purchase.services.coupang_service import stop_sales as coupang_stop

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s [%(threadName)s] %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'backend', 'purchase', 'purchase.db')

CATEGORY_KEYWORDS = ['아로마', '캔들', '향초', '디퓨저', '매직블럭', '세척제', '엔진룸세척', '손소독', '청소용품', '욕실청소']

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

def run_smartstore():
    targets = get_targets('smartstore')
    logger.info(f'smartstore 삭제 대상: {len(targets)}건')
    if not targets:
        return
    conn = sqlite3.connect(DB_PATH, timeout=30)
    success = fail = 0
    for i, t in enumerate(targets, 1):
        ok, err = naver_delete(str(t['channel_product_id']))
        if ok:
            conn.execute('UPDATE listings_pa SET status=? WHERE id=?', ('archived', t['listing_id']))
            success += 1
        else:
            if '삭제된 상품' in err or 'NOT_FOUND' in err:
                conn.execute('UPDATE listings_pa SET status=? WHERE id=?', ('archived', t['listing_id']))
                success += 1
            else:
                logger.warning(f'SS 삭제 실패: {t[channel_product_id]} {err}')
                fail += 1
        if i % 20 == 0:
            conn.commit()
            logger.info(f'smartstore 진행: {i}/{len(targets)} 성공={success} 실패={fail}')
    conn.commit()
    conn.close()
    logger.info(f'smartstore 완료: {len(targets)}건 — 성공={success} 실패={fail}')

def run_coupang():
    targets = get_targets('coupang')
    logger.info(f'coupang 판매중지 대상: {len(targets)}건')
    if not targets:
        return
    conn = sqlite3.connect(DB_PATH, timeout=30)
    success = fail = 0
    for i, t in enumerate(targets, 1):
        ok, err = coupang_stop(str(t['channel_product_id']))
        if ok:
            conn.execute('UPDATE listings_pa SET status=? WHERE id=?', ('archived', t['listing_id']))
            success += 1
        else:
            if '삭제된 상품' in err:
                conn.execute('UPDATE listings_pa SET status=? WHERE id=?', ('archived', t['listing_id']))
                success += 1
            else:
                logger.warning(f'CP 판매중지 실패: {t[channel_product_id]} {err}')
                fail += 1
        if i % 20 == 0:
            conn.commit()
            logger.info(f'coupang 진행: {i}/{len(targets)} 성공={success} 실패={fail}')
    conn.commit()
    conn.close()
    logger.info(f'coupang 완료: {len(targets)}건 — 성공={success} 실패={fail}')

def main():
    t_cp = threading.Thread(target=run_coupang, name='coupang')
    t_ss = threading.Thread(target=run_smartstore, name='smartstore')
    t_cp.start()
    t_ss.start()
    t_cp.join()
    t_ss.join()
    logger.info('양 채널 삭제 완료')

if __name__ == '__main__':
    main()
