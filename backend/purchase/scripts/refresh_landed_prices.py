"""일 1회 할인가 백필 + EAN/UPC 보강.

대상: products WHERE listed coupang unique ASIN (~22,617)

흐름:
  1. ProductsV0.get_competitive_pricing_for_asins (batch 20)
     → products.landed_price_usd, listing_price_usd, shipping_usd, price_fetched_at
     → products.discount_pct = (amazon_price_usd - landed_price_usd) / amazon_price_usd * 100

2026-05-21: CatalogItems EAN/UPC 보강 step 제거.
  - 97% ASIN 이 catalog 에 EAN/UPC 미등록 (private label/OEM).
  - 매일 18K 호출 × 0.55s = 167min, 실 채움 1~수건 → SP-API quota 낭비.

다음 단계 (별도 잡 apply_coupang_discount.py):
  discount_pct >= 20 인 listed coupang 행에 대해 쿠팡 PUT
"""
import sys, os, sqlite3, json, time, logging
from datetime import datetime, timezone
sys.path.insert(0, '/home/ubuntu/CharisG-Platform/charisg-platform')
os.chdir('/home/ubuntu/CharisG-Platform/charisg-platform')
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/CharisG-Platform/charisg-platform/.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.FileHandler('/tmp/refresh_landed_prices.log'),
                              logging.StreamHandler()])
logger = logging.getLogger('refresh_landed')

DB = '/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db'
PRICING_BATCH = 20
PRICING_SLEEP = 1.0


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db_query(sql, params=()):
    for retry in range(5):
        try:
            conn = sqlite3.connect(DB, timeout=300)
            conn.execute('PRAGMA busy_timeout=300000')
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return rows
        except sqlite3.OperationalError as e:
            if 'lock' in str(e).lower() and retry < 4:
                time.sleep(5 + retry * 5); continue
            raise
    return []


def _db_exec_many(sql, params_list):
    if not params_list: return 0
    for retry in range(5):
        try:
            conn = sqlite3.connect(DB, timeout=300)
            conn.execute('PRAGMA busy_timeout=300000')
            conn.executemany(sql, params_list)
            conn.commit(); rc = conn.total_changes; conn.close()
            return rc
        except sqlite3.OperationalError as e:
            if 'lock' in str(e).lower() and retry < 4:
                time.sleep(5 + retry * 5); continue
            raise
    return 0


def _fetch_prices_batch(asins, client):
    """ProductsV0 → {asin: {'landed':float|None, 'listing':float|None, 'shipping':float|None}}."""
    out = {a: {'landed': None, 'listing': None, 'shipping': None} for a in asins}
    try:
        r = client.get_competitive_pricing_for_asins(asin_list=asins, item_condition='New')
        payload = r.payload if hasattr(r, 'payload') else r
        for entry in payload or []:
            asin = entry.get('ASIN') or entry.get('asin')
            if not asin: continue
            prod = entry.get('Product') or {}
            cp = (prod.get('CompetitivePricing') or {}).get('CompetitivePrices') or []
            for p in cp:
                if (p.get('condition') or '').lower() != 'new': continue
                price = p.get('Price') or {}
                lp = price.get('LandedPrice') or {}
                lstp = price.get('ListingPrice') or {}
                shp = price.get('Shipping') or {}
                if lp.get('CurrencyCode') == 'USD' and lp.get('Amount') is not None:
                    out[asin]['landed'] = float(lp['Amount'])
                if lstp.get('CurrencyCode') == 'USD' and lstp.get('Amount') is not None:
                    out[asin]['listing'] = float(lstp['Amount'])
                if shp.get('CurrencyCode') == 'USD' and shp.get('Amount') is not None:
                    out[asin]['shipping'] = float(shp['Amount'])
                break  # 첫 New offer 만
    except Exception as e:
        logger.warning(f'pricing batch 실패 ({asins[0]}~): {type(e).__name__} {str(e)[:200]}')
    return out


def main():
    t0 = time.time()

    # 1. 대상 = listed coupang unique ASIN
    rows = _db_query("""
        SELECT DISTINCT p.id, p.asin, p.amazon_price_usd
        FROM products p JOIN listings_pa lp ON lp.product_id=p.id
        WHERE lp.status='listed' AND lp.channel='coupang'
          AND p.asin IS NOT NULL AND p.asin != ''
        ORDER BY p.id
    """)
    total = len(rows)
    logger.info(f'대상 listed coupang unique ASIN: {total}')

    # ASIN → row 매핑 (unique)
    asin_to_row = {r[1]: r for r in rows}
    unique_asins = list(asin_to_row.keys())
    logger.info(f'unique ASIN: {len(unique_asins)}')

    # SP-API client
    from sp_api.api.products.products_v0 import ProductsV0
    from sp_api.base import Marketplaces
    from backend.dropshipping.services.amazon_sp_api_service import get_credentials
    creds = get_credentials()
    pricing_client = ProductsV0(credentials=creds, marketplace=Marketplaces.US)

    # 2. ProductsV0 batch 처리
    price_updates = []  # (landed, listing, shipping, discount_pct, now, asin)
    snapshot_inserts = []  # (asin, fetched_at, list_price, landed, listing, shipping) — 시계열 보존
    fetched = 0
    no_price = 0
    err = 0

    for i in range(0, len(unique_asins), PRICING_BATCH):
        batch = unique_asins[i:i + PRICING_BATCH]
        result = _fetch_prices_batch(batch, pricing_client)
        for asin, prices in result.items():
            row = asin_to_row.get(asin)
            if not row: continue
            list_price = row[2]  # amazon_price_usd
            landed = prices['landed']
            listing = prices['listing']
            shipping = prices['shipping']
            if landed is None and listing is None:
                no_price += 1
                continue
            # discount_pct: 정가(amazon_price_usd) vs landed 비교 우선, 없으면 listing
            actual = landed if landed is not None else listing
            disc_pct = None
            if list_price and list_price > 0 and actual is not None and actual < list_price:
                disc_pct = round((list_price - actual) / list_price * 100, 2)
            now = _now()
            price_updates.append((landed, listing, shipping, disc_pct, now, asin))
            snapshot_inserts.append((asin, now, list_price, landed, listing, shipping))
            fetched += 1

        if (i // PRICING_BATCH) % 5 == 0:
            elapsed = time.time() - t0
            eta = (len(unique_asins) - i - PRICING_BATCH) / max(i + PRICING_BATCH, 1) * elapsed
            logger.info(f'[pricing {i+len(batch)}/{len(unique_asins)}] '
                        f'fetched={fetched} no_price={no_price} ({elapsed/60:.0f}min ETA {eta/60:.0f}min)')

        # chunk DB write — 100 product 마다
        if len(price_updates) >= 100:
            _db_exec_many(
                """UPDATE products
                   SET landed_price_usd=?, listing_price_usd=?, shipping_usd=?,
                       discount_pct=?, price_fetched_at=?
                   WHERE asin=?""",
                price_updates,
            )
            _db_exec_many(
                """INSERT INTO amazon_price_snapshots
                   (asin, fetched_at, list_price_usd, landed_price_usd, listing_price_usd, shipping_usd)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                snapshot_inserts,
            )
            price_updates = []
            snapshot_inserts = []

        time.sleep(PRICING_SLEEP)

    # 잔여 flush
    if price_updates:
        _db_exec_many(
            """UPDATE products
               SET landed_price_usd=?, listing_price_usd=?, shipping_usd=?,
                   discount_pct=?, price_fetched_at=?
               WHERE asin=?""",
            price_updates,
        )
    if snapshot_inserts:
        _db_exec_many(
            """INSERT INTO amazon_price_snapshots
               (asin, fetched_at, list_price_usd, landed_price_usd, listing_price_usd, shipping_usd)
               VALUES (?, ?, ?, ?, ?, ?)""",
            snapshot_inserts,
        )

    logger.info(f'=== 완료 — pricing fetched={fetched} no_price={no_price} ===')
    # 2026-05-21: CatalogItems EAN/UPC 보강 step 제거.
    # 30 sample 진단 결과 97% ASIN 이 Amazon catalog 에 EAN/UPC 미등록 (private label/OEM).
    # 매일 18K 호출 × 0.55s = 167min, 실제 채워지는 건 1~수건 → SP-API quota 낭비.
    # 필요 시 별도 weekly 스크립트로 재도입.


if __name__ == '__main__':
    main()
