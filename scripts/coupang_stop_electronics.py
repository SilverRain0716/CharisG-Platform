"""쿠팡 전자기기+도검류 판매중지."""
import sqlite3, sys, logging
sys.path.insert(0, '.')
from backend.purchase.services.coupang_service import stop_sales

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

conn = sqlite3.connect('backend/purchase/purchase.db', timeout=30)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT l.id, l.channel_product_id FROM listings_pa l
    JOIN products p ON l.product_id = p.id
    LEFT JOIN naver_categories nc ON p.category_path = nc.id
    WHERE l.channel = 'coupang' AND l.status = 'listed'
      AND (
        nc.whole_name LIKE '디지털/가전>모니터%'
        OR nc.whole_name LIKE '디지털/가전>노트북%'
        OR nc.whole_name LIKE '디지털/가전>멀티미디어%'
        OR nc.whole_name LIKE '디지털/가전>PC부품%'
        OR nc.whole_name LIKE '디지털/가전>휴대폰%'
        OR nc.whole_name LIKE '디지털/가전>영상가전%'
        OR nc.whole_name LIKE '%다용도칼/멀티툴%'
      )
""").fetchall()

logger.info(f'CP 삭제 대상: {len(rows)}건')
ok = fail = 0
for i, r in enumerate(rows, 1):
    cpid = str(r['channel_product_id'])
    success, err = stop_sales(cpid)
    if success or '삭제된 상품' in err:
        conn.execute('UPDATE listings_pa SET status=?  WHERE id=?', ('archived', r['id']))
        ok += 1
    else:
        logger.warning(f'실패: {cpid} {err}')
        fail += 1
    if i % 50 == 0:
        conn.commit()
        logger.info(f'진행: {i}/{len(rows)} 성공={ok} 실패={fail}')
conn.commit()
conn.close()
logger.info(f'완료: 성공={ok} 실패={fail}')
