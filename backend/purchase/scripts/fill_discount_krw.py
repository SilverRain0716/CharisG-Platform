"""fill_discount_krw.py — 20%+ Amazon 할인 상품의 우리 할인가 산정 + 쿠팡 PUT.

논리:
  sale_krw     = 우리 가격 공식(amazon_price_usd as cost)  ← 기존 정가
  discount_krw = 우리 가격 공식(landed_price_usd as cost) ← 신규 할인가

쿠팡 PUT:
  originalPrice = sale_krw     (정가, 빨간 줄)
  salePrice     = discount_krw (할인가, 실제 결제)

대상:
  listings_pa.channel='coupang' AND status='listed'
  JOIN products WHERE amazon_price_usd > 0
                AND landed_price_usd IS NOT NULL
                AND landed_price_usd < amazon_price_usd
                AND (amazon_price_usd - landed_price_usd) / amazon_price_usd >= 0.20

안전장치:
  - our_discount_pct >= 5%: 너무 작은 할인 noise 제외
  - our_discount_pct <= 50%: MSRP 인플레 케이스 자동 제외
  - 마진 자동 35% (양쪽 다)
"""
import sys, os, sqlite3, math, argparse, time, logging
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '/home/ubuntu/CharisG-Platform/charisg-platform')
os.chdir('/home/ubuntu/CharisG-Platform/charisg-platform')
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/CharisG-Platform/charisg-platform/.env')
# ★계정 선택 (--account new|old, 기본 old) — coupang_service COUPANG_ACTIVE 상수 확정 전에 세팅
_ACCT = 'old'
if '--account' in sys.argv:
    try:
        _ACCT = sys.argv[sys.argv.index('--account') + 1]
    except Exception:
        _ACCT = 'old'
if _ACCT not in ('old', 'new'):
    _ACCT = 'old'
os.environ['COUPANG_ACTIVE'] = _ACCT

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.FileHandler('/tmp/fill_discount_krw.log'),
                              logging.StreamHandler()])
logger = logging.getLogger('fill_discount')

DB = '/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db'
SLEEP_API = 0.7
OUR_DISCOUNT_MIN = 0.05  # 5% 미만 noise
OUR_DISCOUNT_MAX = 0.50  # 50% 초과 inflated MSRP


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _round100(n):
    return int(math.ceil(n / 100.0) * 100)


def _calc_sale_price(cost_usd, channel, s):
    """cost_usd 와 channel 기반 sale_krw 역산 (기존 backfill_amazon_price.py 동일)."""
    fx = s['fx']
    fee_rate = s['cp_fee'] if channel == 'coupang' else s['ss_fee']
    forwarder = s['forwarder']
    cs_cost = s['cs_cost']
    return_pct = s['return_pct']
    margin = s['margin_target']
    shipping = 3000
    total_cost = cost_usd * fx + forwarder + cs_cost + shipping
    denom = 1.0 - margin - fee_rate - return_pct
    if denom <= 0:
        return None
    return _round100(total_cost / denom)


def _db_query(sql, params=()):
    for retry in range(5):
        try:
            conn = sqlite3.connect(DB, timeout=300)
            conn.execute('PRAGMA busy_timeout=300000')
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError as e:
            if 'lock' in str(e).lower() and retry < 4:
                time.sleep(5 + retry * 5); continue
            raise
    return []


def _db_exec(sql, params=()):
    for retry in range(5):
        try:
            conn = sqlite3.connect(DB, timeout=300)
            conn.execute('PRAGMA busy_timeout=300000')
            conn.execute(sql, params)
            conn.commit(); conn.close()
            return True
        except sqlite3.OperationalError as e:
            if 'lock' in str(e).lower() and retry < 4:
                time.sleep(5 + retry * 5); continue
            raise
    return False


def _other_job_active(window_sec: int = 60) -> bool:
    """listings_pa 가 최근 N초 내 갱신된 row 가 있으면 = 다른 잡(lister/sync) 활성."""
    rows = _db_query(
        "SELECT COUNT(*) AS cnt FROM listings_pa WHERE last_synced_at > datetime('now', ?)",
        (f'-{window_sec} seconds',),
    )
    return bool(rows and rows[0].get('cnt', 0) > 0)


def _wait_for_idle(max_wait_h: int = 12, check_interval: int = 15, idle_required: int = 2, max_wait_sec: int = 120) -> bool:
    """다른 잡 idle 될 때까지 대기 (turnstile 락을 쥔 채 호출되므로 상한 필수).

    2026-06-05 픽스: 과거 max_wait_h=12 + check_interval=180 이라 리스터/sync 가 길게 돌면
    락을 쥔 채 최대 12h 대기 → 다른 배치잡 herd-kill (quota-retry 300s 타임아웃 사망).
    이제 max_wait_sec(기본 120s, 300s 락타임아웃 미만)으로 상한 → 락 장기점유 불가.
    """
    consecutive_idle = 0
    deadline = time.time() + min(max_wait_h * 3600, max_wait_sec)
    while time.time() < deadline:
        if _other_job_active():
            consecutive_idle = 0
            logger.info(f'다른 잡 활성 (lister/sync) — {check_interval}s 대기')
            time.sleep(check_interval)
        else:
            consecutive_idle += 1
            logger.info(f'idle 감지 {consecutive_idle}/{idle_required}')
            if consecutive_idle >= idle_required:
                logger.info('idle 확정 — 진행')
                return True
            time.sleep(check_interval)
    logger.warning(f'idle 대기 {max_wait_sec}s 상한 도달 — 강제 진행 (락 장기점유 방지)')
    return False


# ── 가격변경 ±한도(현재가 -50% 인하 / +100% 인상) 초과 거부 시 live 현재가 읽어 한 칸 step ──
# 2026-06-11: 단일 PUT 한도 초과(딥디스카운트 인하 / 정가복귀 인상)를 여러 실행에 나눠 수렴.
_LIMIT_MARKS = ('변경전 판매가', '50%', '100%', '인상', '인하')


def _get_live_sale_price(sp_id, vid):
    """get_seller_product → data.items[*] 에서 vendorItemId==vid 의 현재 salePrice (없으면 첫 item)."""
    from backend.purchase.services import coupang_service
    try:
        d = coupang_service.get_seller_product(sp_id) or {}
        data = d.get('data') or d
        items = data.get('items') or []
        for it in items:
            if str(it.get('vendorItemId')) == str(vid) and it.get('salePrice'):
                return int(it['salePrice'])
        if items and items[0].get('salePrice'):
            return int(items[0]['salePrice'])
    except Exception as e:
        logger.warning(f'[live-price] sp={sp_id} vid={vid}: {e}')
    return None


def _put_sale_stepped(sp_id, vid, target):
    """salePrice 를 target 으로 PUT. 쿠팡 ±한도 거부면 live 현재가 읽어 한 칸(±limit)만 step.

    반환 (성공, 적용가, 완전도달?). 한도무관 에러/진전불가는 (False, *, False).
    부분 step 이면 호출측이 적용가를 discount_krw 에 기록 → 다음 실행이 그 값에서 이어감(멀티런 수렴).
    """
    from backend.purchase.services import coupang_service
    target = int(target)
    ok, msg = coupang_service.update_vendor_item_price(vid, target)
    time.sleep(SLEEP_API)
    if ok:
        return True, target, True
    if not any(m in (msg or '') for m in _LIMIT_MARKS):
        return False, None, False  # 한도와 무관한 에러 → step 안 함
    live = _get_live_sale_price(sp_id, vid)
    if not live or live <= 0:
        return False, None, False
    if target < live:                      # 인하: step down (현재가 -50% 한도 → live*0.5)
        step = max(target, math.ceil(live * 0.51))
    else:                                  # 인상: step up (현재가 +100% 한도 → live*2.0)
        step = min(target, int(live * 1.95))
    if step == live or step <= 0:
        return False, live, False          # 진전 불가(현재가가 이미 목표 근처) = stuck
    ok2, _m2 = coupang_service.update_vendor_item_price(vid, step)
    time.sleep(SLEEP_API)
    if ok2:
        return True, step, (step == target)
    return False, None, False


def _load_settings():
    rows = _db_query("SELECT key, value FROM settings")
    s = {r['key']: r['value'] for r in rows}
    def f(k, default):
        v = s.get(k)
        try:
            return float(v) if v not in (None, '') else default
        except Exception:
            return default
    return {
        'fx': f('exchange_rate_usd_krw', f('margin.default_fx_rate', 1465.0)),
        'forwarder': f('margin.forwarder_fee_krw', 5000),
        'cs_cost': f('margin.cs_cost_krw', 2000),
        'return_pct': f('margin.return_reserve_pct', 0.0) / 100.0,
        'cp_fee': f('coupang_fee_rate', 0.11),
        'ss_fee': f('smartstore_fee_rate', 0.0548),
        'margin_target': f('margin_target_rate', 0.35),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='실제 PUT (없으면 dry-run)')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--account', choices=['old', 'new'], default='old', help='대상 쿠팡 계정')
    args = ap.parse_args()
    apply_changes = args.apply

    s = _load_settings()
    logger.info(f'settings: {s}')

    # 시작 전 idle 대기 (다른 잡 끝날 때까지)
    if apply_changes:
        logger.info('=== 시작 전 idle 대기 ===')
        _wait_for_idle()

    sql = """
        SELECT lp.id AS lid, lp.channel_product_id, lp.sale_krw,
               lp.discount_krw AS prev_discount_krw, lp.discount_synced_at AS prev_synced,
               p.id AS pid, p.asin, p.amazon_price_usd, p.landed_price_usd,
               p.discount_pct AS amazon_disc_pct,
               substr(coalesce(p.title_ko, p.title_en), 1, 35) AS title
        FROM listings_pa lp JOIN products p ON p.id = lp.product_id
        WHERE lp.channel='coupang' AND lp.status='listed'
          AND COALESCE(lp.price_mode,'auto')<>'manual'
          AND lp.channel_product_id IS NOT NULL AND lp.channel_product_id != ''
          AND lp.sale_krw IS NOT NULL AND lp.sale_krw > 0
          AND p.amazon_price_usd IS NOT NULL AND p.amazon_price_usd > 0
          AND p.landed_price_usd IS NOT NULL AND p.landed_price_usd > 0
          AND p.landed_price_usd < p.amazon_price_usd
          AND (p.amazon_price_usd - p.landed_price_usd) / p.amazon_price_usd >= 0.20
        ORDER BY p.id
    """
    if args.limit:
        sql += f" LIMIT {args.limit}"
    sql = sql.replace("lp.status='listed'", "lp.status='listed' AND lp.coupang_account='" + _ACCT + "'")
    rows = _db_query(sql)
    total = len(rows)
    logger.info(f'대상: {total}건  |  모드: {"APPLY" if apply_changes else "DRY-RUN"}')
    logger.info('=' * 100)

    from backend.purchase.services import coupang_service

    cnt_skip_too_small = 0
    cnt_skip_too_big = 0
    cnt_skip_invalid = 0
    cnt_no_vid = 0
    cnt_put_ok = 0
    cnt_put_step = 0
    cnt_put_err = 0
    cnt_skip_unchanged = 0
    # 30일 이내 같은 할인가가 이미 적용됐으면 재PUT 생략(효율). 30일 지나면 재확인차 강제 재적용.
    resync_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    PER_RUN_PUT_CAP = 10000  # 2026-06-05 per-run 상한 — 락 장기점유 방지 (백로그는 다음 실행이 이어 처리)
    for idx, r in enumerate(rows, 1):
        if cnt_put_ok + cnt_put_err >= PER_RUN_PUT_CAP:
            logger.info(f'[fill-discount] per-run PUT 상한 {PER_RUN_PUT_CAP:,} 도달 — 중단 (스캔 {idx}/{total}, 나머지 다음 실행)')
            break
        # 우리 할인가 계산 — landed_price_usd 를 cost 로 보고 마진 공식 적용
        new_discount_krw = _calc_sale_price(r['landed_price_usd'], 'coupang', s)
        if not new_discount_krw or new_discount_krw <= 0:
            cnt_skip_invalid += 1
            continue

        sale_krw = r['sale_krw']
        if new_discount_krw >= sale_krw:
            cnt_skip_invalid += 1
            continue

        our_disc_pct = (sale_krw - new_discount_krw) / sale_krw
        our_disc_pct_rounded = round(our_disc_pct * 100, 1)

        # 안전장치
        if our_disc_pct < OUR_DISCOUNT_MIN:
            cnt_skip_too_small += 1
            continue
        if our_disc_pct > OUR_DISCOUNT_MAX:
            cnt_skip_too_big += 1
            if cnt_skip_too_big <= 5:
                logger.info(f'[skip-inflated] pid={r["pid"]} {r["asin"]} '
                            f'amazon ${r["amazon_price_usd"]:.0f}→${r["landed_price_usd"]:.2f} '
                            f'우리 {sale_krw:,}→{new_discount_krw:,} ({our_disc_pct_rounded}% > 50%)')
            continue

        # ── 효율: 이미 같은 할인가가 최근(30일내) 적용됨 → 쿠팡 PUT 생략 (변동분만 처리) ──
        if (r['prev_discount_krw'] is not None and r['prev_synced'] is not None
                and r['prev_synced'] >= resync_cutoff
                and abs(new_discount_krw - r['prev_discount_krw']) < 1):
            cnt_skip_unchanged += 1
            continue

        if not apply_changes:
            if idx <= 30 or idx % 100 == 0:
                logger.info(f'[DRY] {r["asin"]} amazon ${r["amazon_price_usd"]:.0f}→${r["landed_price_usd"]:.2f} '
                            f'({r["amazon_disc_pct"]:.0f}%) | 우리 {sale_krw:,}→{new_discount_krw:,} '
                            f'({our_disc_pct_rounded}%) | {r["title"]}')
            cnt_put_ok += 1
            continue

        # 쿠팡 PUT — vendor_item_id 추출
        sp_id = r['channel_product_id']
        try:
            vids = coupang_service.get_vendor_item_ids(sp_id)
        except Exception as e:
            logger.warning(f'get_vendor_item_ids 실패 sp_id={sp_id}: {e}')
            vids = []
        if not vids:
            cnt_no_vid += 1
            continue

        ok_all = True
        all_full = True
        rec_sale = new_discount_krw   # 부분 step 이면 적용된 중간값(target보다 높음)을 기록
        last_err = ''
        for vid in vids:
            ok1, msg1 = coupang_service.update_vendor_item_original_price(vid, sale_krw)
            time.sleep(SLEEP_API)
            if not ok1:
                ok_all = False; last_err = f'orig: {msg1[:120]}'
                continue
            ok2, applied2, full2 = _put_sale_stepped(sp_id, vid, new_discount_krw)
            if not ok2:
                ok_all = False; last_err = 'sale: step/put 실패(한도/진전불가)'
            elif not full2:
                all_full = False
                rec_sale = max(rec_sale, applied2)   # 인하 중간값(target보다 높음)

        if ok_all:
            if all_full:
                cnt_put_ok += 1
            else:
                cnt_put_step += 1   # 부분 인하 — 다음 실행에 이어서 더 내림
            rec_pct = round((sale_krw - rec_sale) / sale_krw * 100) if sale_krw else our_disc_pct_rounded
            _db_exec(
                """UPDATE listings_pa SET discount_krw=?, discount_pct=?, discount_synced_at=?
                   WHERE id=?""",
                (rec_sale, rec_pct, _now(), r['lid']),
            )
        else:
            cnt_put_err += 1
            if cnt_put_err <= 10:
                logger.warning(f'[put-err] pid={r["pid"]} sp_id={sp_id} {last_err}')

        if idx % 50 == 0:
            logger.info(f'[{idx}/{total}] put_ok={cnt_put_ok} put_err={cnt_put_err} '
                        f'no_vid={cnt_no_vid} too_small={cnt_skip_too_small} too_big={cnt_skip_too_big}')

        # 매 100건마다 다른 잡 활성 재감지 — 활성이면 idle 될 때까지 대기 후 재개
        if idx % 100 == 0 and apply_changes and _other_job_active():
            logger.warning('PUT 도중 lister/sync 활성 감지 — idle 대기')
            _wait_for_idle()
            logger.info('재개')

    # ═══ Pass 2: 할인 종료 복귀 ═══
    # discount_krw 적용됐는데 더 이상 Amazon 20%+ 할인이 아님(landed 회복) → 쿠팡 가격을 정가(sale_krw)로 복귀.
    # 현재가 데이터(landed) 있을 때만 복귀(없으면 보수적으로 유지). 복귀=가격인상이나 할인캡 50%라 ≤+100%, 쿠팡 한도내.
    cnt_revert_ok = 0
    cnt_revert_step = 0
    cnt_revert_err = 0
    revert_sql = """
        SELECT lp.id AS lid, lp.channel_product_id, lp.sale_krw, lp.discount_krw,
               p.id AS pid, p.asin
        FROM listings_pa lp JOIN products p ON p.id = lp.product_id
        WHERE lp.channel='coupang' AND lp.status='listed'
          AND COALESCE(lp.price_mode,'auto')<>'manual'
          AND lp.channel_product_id IS NOT NULL AND lp.channel_product_id != ''
          AND lp.sale_krw IS NOT NULL AND lp.sale_krw > 0
          AND lp.discount_krw IS NOT NULL AND lp.discount_krw > 0
          AND p.amazon_price_usd IS NOT NULL AND p.amazon_price_usd > 0
          AND p.landed_price_usd IS NOT NULL AND p.landed_price_usd > 0
          AND NOT (p.landed_price_usd < p.amazon_price_usd
                   AND (p.amazon_price_usd - p.landed_price_usd) / p.amazon_price_usd >= 0.20)
        ORDER BY p.id
    """
    if args.limit:
        revert_sql += f" LIMIT {args.limit}"
    revert_sql = revert_sql.replace("lp.status='listed'", "lp.status='listed' AND lp.coupang_account='" + _ACCT + "'")
    revert_rows = _db_query(revert_sql)
    logger.info('=' * 100)
    logger.info(f'=== Pass2 할인종료 복귀 대상: {len(revert_rows)}건  |  '
                f'모드: {"APPLY" if apply_changes else "DRY-RUN"} ===')
    for ridx, r in enumerate(revert_rows, 1):
        if not apply_changes:
            if ridx <= 20:
                logger.info(f'[DRY-revert] {r["asin"]} 할인가 {r["discount_krw"]:,} → 정가 {r["sale_krw"]:,}')
            cnt_revert_ok += 1
            continue
        try:
            vids = coupang_service.get_vendor_item_ids(r['channel_product_id'])
        except Exception as e:
            logger.warning(f'[revert] get_vendor_item_ids 실패 sp_id={r["channel_product_id"]}: {e}')
            vids = []
        if not vids:
            cnt_revert_err += 1
            continue
        ok_all = True
        all_full = True
        rec_rev = r['sale_krw']   # 부분 step 이면 적용된 중간값(target=정가보다 낮음)을 기록
        for vid in vids:
            ok, applied, full = _put_sale_stepped(r['channel_product_id'], vid, r['sale_krw'])
            if not ok:
                ok_all = False
                if cnt_revert_err < 10:
                    logger.warning(f'[revert-err] {r["asin"]} vid={vid} (한도/진전불가)')
            elif not full:
                all_full = False
                rec_rev = min(rec_rev, applied)   # 인상 중간값(정가보다 낮음)
        if ok_all and all_full:
            cnt_revert_ok += 1
            _db_exec("UPDATE listings_pa SET discount_krw=NULL, discount_pct=NULL, discount_synced_at=? WHERE id=?",
                     (_now(), r['lid']))
        elif ok_all:
            cnt_revert_step += 1   # 부분 인상 — discount_krw 에 중간값 기록 → 다음 실행에 이어서 더 올림(정가까지)
            rec_pct = max(0, round((r['sale_krw'] - rec_rev) / r['sale_krw'] * 100)) if r['sale_krw'] else 0
            _db_exec("UPDATE listings_pa SET discount_krw=?, discount_pct=?, discount_synced_at=? WHERE id=?",
                     (rec_rev, rec_pct, _now(), r['lid']))
        else:
            cnt_revert_err += 1
        if ridx % 50 == 0:
            logger.info(f'[revert {ridx}/{len(revert_rows)}] ok={cnt_revert_ok} err={cnt_revert_err}')
        if ridx % 100 == 0 and apply_changes and _other_job_active():
            logger.warning('[revert] 다른 잡 활성 — idle 대기')
            _wait_for_idle()
            logger.info('[revert] 재개')

    logger.info('=' * 100)
    logger.info(f'=== 완료 — '
                f'put_ok={cnt_put_ok} put_err={cnt_put_err} '
                f'too_small={cnt_skip_too_small} too_big={cnt_skip_too_big} '
                f'invalid={cnt_skip_invalid} no_vid={cnt_no_vid} '
                f'unchanged(재PUT생략)={cnt_skip_unchanged} put_step(부분인하)={cnt_put_step} | '
                f'revert_ok={cnt_revert_ok} revert_step(부분인상)={cnt_revert_step} revert_err={cnt_revert_err} ===')


if __name__ == '__main__':
    main()
