"""상품 가격 검수 + 마진 재계산 — SP-API 가격 교정 + 판매가 역산.

사용법:
  python3 scripts/audit_prices.py --full --fix

개선 (v2):
  - 100건마다 commit (중단 안전)
  - 이미 처리된 건 건너뛰기 (amazon_price_usd IS NOT NULL)
  - 마진 공식으로 sale_price_krw + listings_pa 재계산
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import time

sys.path.insert(0, ".")

with open(".env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from backend.purchase.services.image_downloader import fetch_product_info_sp_api

DB_PATH = "backend/purchase/purchase.db"
COMMIT_INTERVAL = 100


# ── 마진 계산 (pricing_service_pa 로직 인라인) ──

def _load_pricing_settings(conn):
    """settings 테이블에서 가격 계산용 파라미터 로드."""
    keys = [
        "margin_target_rate", "smartstore_fee_rate", "coupang_fee_rate",
        "exchange_rate_usd_krw",
        "margin.forwarder_fee_krw", "margin.cs_cost_krw", "margin.return_reserve_pct",
    ]
    out = {}
    for k in keys:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        if row:
            out[k] = float(row["value"])
    return out


def _calc_sale_price(cost_usd, channel, settings):
    """cost_usd → sale_price_krw (채널별 마진 역산, 100원 단위)."""
    fx = settings.get("exchange_rate_usd_krw", 1477.0)
    margin_rate = settings.get("margin_target_rate", 0.35)
    forwarder = settings.get("margin.forwarder_fee_krw", 5000)
    cs_cost = settings.get("margin.cs_cost_krw", 2000)
    return_pct = settings.get("margin.return_reserve_pct", 3) / 100.0
    shipping = 3000  # 국내 배송비 고정

    if channel == "smartstore":
        fee_rate = settings.get("smartstore_fee_rate", 0.0548)
    else:
        fee_rate = settings.get("coupang_fee_rate", 0.1374)

    total_cost = cost_usd * fx + forwarder + cs_cost + shipping
    denom = 1.0 - margin_rate - fee_rate - return_pct
    if denom <= 0:
        return None
    sale_raw = total_cost / denom
    return int(math.ceil(sale_raw / 100) * 100)


def _weight_to_grams(dims):
    """dimensions dict에서 weight_g 변환."""
    if not dims or dims.get("weight") is None:
        return None
    try:
        w_val = float(dims["weight"])
        w_unit = (dims.get("weight_unit") or "").lower()
        if "pound" in w_unit or w_unit == "lb":
            return int(w_val * 453.592)
        elif "ounce" in w_unit or w_unit == "oz":
            return int(w_val * 28.3495)
        elif "kilogram" in w_unit or w_unit == "kg":
            return int(w_val * 1000)
        elif "gram" in w_unit or w_unit == "g":
            return int(w_val)
        else:
            return int(w_val * 453.592)
    except (ValueError, TypeError):
        return None


def audit(sample_size: int, do_fix: bool):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    # 이미 처리된 건 건너뛰기
    skip_clause = "AND amazon_price_usd IS NULL" if do_fix else ""

    if sample_size == 0:
        rows = conn.execute(
            f"""SELECT id, asin, cost_usd, amazon_price_usd, sale_price_krw, title_en
               FROM products
               WHERE business_model='purchase' AND status != 'removed'
                 AND asin IS NOT NULL {skip_clause}
               ORDER BY id"""
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT id, asin, cost_usd, amazon_price_usd, sale_price_krw, title_en
               FROM products
               WHERE business_model='purchase' AND status != 'removed'
                 AND asin IS NOT NULL {skip_clause}
               ORDER BY RANDOM() LIMIT ?""",
            (sample_size,),
        ).fetchall()

    total = len(rows)
    if total == 0:
        print("처리할 상품 없음 (전부 교정 완료)")
        conn.close()
        return

    # 가격 설정 로드
    settings = _load_pricing_settings(conn)
    fx = settings.get("exchange_rate_usd_krw", 1477.0)
    print(f"검수 대상: {total}건  |  환율: {fx}  |  마진: {settings.get('margin_target_rate', 0.35)*100:.0f}%")
    print(f"{'='*100}")

    stats = {
        "checked": 0, "match": 0, "mismatch": 0,
        "no_api_price": 0, "api_fail": 0, "updated": 0,
        "price_recalced": 0,
    }
    mismatches = []

    for idx, r in enumerate(rows, 1):
        pid = r["id"]
        asin = r["asin"]
        db_cost = r["cost_usd"] or 0.0
        db_sale = r["sale_price_krw"] or 0
        title = (r["title_en"] or "")[:50]

        sp = fetch_product_info_sp_api(asin)
        stats["checked"] += 1

        if not sp:
            stats["api_fail"] += 1
            print(f"[{idx}/{total}] #{pid} {asin} — SP-API 실패")
            time.sleep(0.55)
            continue

        api_price = sp.get("amazon_price_usd")
        dims = sp.get("dimensions")
        identifiers = sp.get("identifiers")
        classifications = sp.get("classifications")
        weight_g = _weight_to_grams(dims)

        if api_price is None:
            stats["no_api_price"] += 1
            if do_fix:
                # 부가정보만 업데이트
                conn.execute(
                    """UPDATE products SET
                         dimensions_json = COALESCE(?, dimensions_json),
                         identifiers_json = COALESCE(?, identifiers_json),
                         amazon_category_json = COALESCE(?, amazon_category_json),
                         weight_g = COALESCE(?, weight_g),
                         updated_at = datetime('now')
                       WHERE id = ?""",
                    (
                        json.dumps(dims, ensure_ascii=False) if dims else None,
                        json.dumps(identifiers, ensure_ascii=False) if identifiers else None,
                        json.dumps(classifications, ensure_ascii=False) if classifications else None,
                        weight_g, pid,
                    ),
                )
            time.sleep(0.55)
            continue

        diff_pct = abs(api_price - db_cost) / max(api_price, 0.01) * 100

        if diff_pct <= 5:
            stats["match"] += 1
            label = "✓"
        else:
            stats["mismatch"] += 1
            label = "✗"
            mismatches.append({
                "id": pid, "asin": asin, "title": title,
                "db_cost": db_cost, "api_price": api_price,
                "diff_pct": round(diff_pct, 1),
            })

        if do_fix:
            # 1) products 업데이트 (가격 + 부가정보)
            new_sale_ss = _calc_sale_price(api_price, "smartstore", settings)
            new_sale_cp = _calc_sale_price(api_price, "coupang", settings)
            # products.sale_price_krw 는 스마트스토어 기준
            new_sale = new_sale_ss or db_sale

            new_margin_pct = None
            if new_sale and api_price > 0:
                cost_krw = api_price * fx
                forwarder = settings.get("margin.forwarder_fee_krw", 5000)
                cs_cost = settings.get("margin.cs_cost_krw", 2000)
                return_rsv = new_sale * settings.get("margin.return_reserve_pct", 3) / 100
                net = new_sale - cost_krw - forwarder - cs_cost - return_rsv
                new_margin_pct = round(net / new_sale * 100, 2)

            conn.execute(
                """UPDATE products SET
                     amazon_price_usd = ?,
                     cost_usd = ?,
                     sale_price_krw = ?,
                     margin_pct = ?,
                     dimensions_json = ?,
                     identifiers_json = ?,
                     amazon_category_json = ?,
                     weight_g = COALESCE(?, weight_g),
                     updated_at = datetime('now')
                   WHERE id = ?""",
                (
                    api_price, api_price, new_sale, new_margin_pct,
                    json.dumps(dims, ensure_ascii=False) if dims else None,
                    json.dumps(identifiers, ensure_ascii=False) if identifiers else None,
                    json.dumps(classifications, ensure_ascii=False) if classifications else None,
                    weight_g, pid,
                ),
            )

            # 2) listings_pa 채널별 판매가 업데이트
            listings = conn.execute(
                "SELECT id, channel FROM listings_pa WHERE product_id = ?", (pid,)
            ).fetchall()
            for lst in listings:
                ch = lst["channel"]
                ch_sale = new_sale_ss if ch == "smartstore" else new_sale_cp
                if ch_sale:
                    # net_margin 재계산
                    cost_krw = api_price * fx
                    forwarder = settings.get("margin.forwarder_fee_krw", 5000)
                    cs_cost = settings.get("margin.cs_cost_krw", 2000)
                    return_rsv = ch_sale * settings.get("margin.return_reserve_pct", 3) / 100
                    fee_rate = settings.get("smartstore_fee_rate", 0.0548) if ch == "smartstore" else settings.get("coupang_fee_rate", 0.1374)
                    ch_fee = ch_sale * fee_rate
                    net = ch_sale - cost_krw - forwarder - cs_cost - return_rsv - ch_fee
                    margin_pct = round(net / ch_sale * 100, 2) if ch_sale else 0

                    conn.execute(
                        """UPDATE listings_pa SET
                             sale_krw = ?,
                             net_margin_krw = ?,
                             net_margin_pct = ?,
                             margin_risk = ?
                           WHERE id = ?""",
                        (
                            ch_sale,
                            int(net),
                            margin_pct,
                            "safe" if margin_pct >= 20 else ("warning" if margin_pct >= 10 else "danger"),
                            lst["id"],
                        ),
                    )
            stats["updated"] += 1
            stats["price_recalced"] += 1

        sale_info = ""
        if do_fix and api_price:
            new_s = _calc_sale_price(api_price, "smartstore", settings)
            sale_info = f"  판매가=₩{new_s:,}" if new_s else ""

        print(f"[{idx}/{total}] {label} #{pid} {asin}  DB=${db_cost:.2f}  API=${api_price:.2f}  차이={diff_pct:.1f}%{sale_info}  {title}")

        # 주기적 commit
        if do_fix and idx % COMMIT_INTERVAL == 0:
            conn.commit()
            print(f"  💾 {idx}건 커밋 완료")

        time.sleep(0.55)

    conn.commit()
    conn.close()

    # 요약
    print(f"\n{'='*100}")
    print(f"검수 완료: {stats['checked']}건")
    print(f"  일치 (5% 이내): {stats['match']}건")
    print(f"  불일치 (5% 초과): {stats['mismatch']}건")
    print(f"  API 가격 없음: {stats['no_api_price']}건")
    print(f"  API 호출 실패: {stats['api_fail']}건")
    if do_fix:
        print(f"  DB 업데이트: {stats['updated']}건")
        print(f"  판매가 재계산: {stats['price_recalced']}건")

    if mismatches:
        print(f"\n불일치 TOP 30 (차이 큰 순):")
        mismatches.sort(key=lambda x: -x["diff_pct"])
        for m in mismatches[:30]:
            print(f"  #{m['id']} {m['asin']}  DB=${m['db_cost']:.2f} → API=${m['api_price']:.2f}  ({m['diff_pct']:+.1f}%)  {m['title']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="상품 가격 검수 + 마진 재계산")
    parser.add_argument("--sample", type=int, default=50, help="샘플 수 (기본 50)")
    parser.add_argument("--full", action="store_true", help="전체 검수")
    parser.add_argument("--fix", action="store_true", help="차이 발견 시 DB 업데이트 + 마진 재계산")
    args = parser.parse_args()

    sample = 0 if args.full else args.sample
    audit(sample, args.fix)
