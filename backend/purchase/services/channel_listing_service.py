"""PA 채널 리스팅 생성 — products → listings_pa (가격 이원화).

재발송 방지: listings_pa_archived 에 upload_failure / past_upload_failure 로 기록된
(product_id, channel) 조합은 다시 listings_pa 에 INSERT/UPSERT 하지 않는다.
사용자가 실패 상품을 정리 후 "완전 제외" 한 건은 다음 '채널 보내기' 에도 부활하지 않게.

2026-05-15: ai_processor.resolve_category 실패율 ↑ 로 products.category_path 가
비어있는 경우 다수 발생. send_to_channels 단에서 keyword_category_map cache fallback
+ listings_pa.coupang_category_code 도 함께 채워서 양 채널 모두 우리 시스템이 정확한
카테고리로 등록되도록 보강 (이전엔 쿠팡 자동매칭 displayCategoryCode=0 에 의존).
"""
import logging

from backend.purchase.database import get_db
from backend.purchase.services import clean_policy, safety_filter
from backend.purchase.services.forwarder_shipping import forwarder_shipping_usd
from backend.purchase.services.pricing_service_pa import calculate_sale_krw

logger = logging.getLogger(__name__)


_SKIP_ARCHIVED_REASONS = ("upload_failure", "past_upload_failure")


def _load_default_forwarder_extras() -> dict:
    """v37 — 신규 listing 등록 시 Forwarder 경로 비용 보강.

    신규 listing 은 아직 kr_shipping_eligible 미검증이라 default Forwarder 가정.
    이후 kr_shipping_verifier 가 eligible=0 마크하면 forwarder_pricing 이 정확한
    LBS 요금으로 재계산. eligible=1 마크되면 sale_krw 그대로 (가격 인하 없음 정책).
    """
    keys = ("pricing.safety_margin_krw", "margin.cs_cost_krw", "margin.return_reserve_pct")
    out: dict = {}
    with get_db() as conn:
        for k in keys:
            r = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
            if r and r["value"] not in (None, ""):
                try:
                    out[k] = float(r["value"])
                except (TypeError, ValueError):
                    pass
    return {
        "safety_krw":  out.get("pricing.safety_margin_krw", 5000.0),
        "cs_krw":      out.get("margin.cs_cost_krw",        2000.0),
        "return_pct":  out.get("margin.return_reserve_pct", 0.0) / 100.0,
    }


def _resolve_categories_via_cache(product_id: int) -> tuple[str | None, int | None]:
    """product_keywords 의 키워드로 keyword_category_map 조회 → (naver_id, coupang_code).

    is_primary 우선, 그 다음 등록 순. 매핑 없으면 (None, None).
    """
    with get_db() as conn:
        kw = conn.execute(
            """SELECT m.naver_category_id, m.coupang_category_code
                 FROM product_keywords pk
                 JOIN keyword_category_map m ON m.keyword = pk.keyword
                WHERE pk.product_id=?
                  AND (m.naver_category_id IS NOT NULL OR m.coupang_category_code IS NOT NULL)
                ORDER BY pk.is_primary DESC, pk.id ASC
                LIMIT 1""",
            (product_id,),
        ).fetchone()
    if not kw:
        return None, None
    return kw["naver_category_id"], kw["coupang_category_code"]


def _is_archived_failure(product_id: int, channel: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            """SELECT 1 FROM listings_pa_archived
               WHERE product_id=? AND channel=? AND archived_reason IN (?, ?)
               LIMIT 1""",
            (product_id, channel, *_SKIP_ARCHIVED_REASONS),
        ).fetchone()
    return row is not None


def send_to_channels(product_id: int, channels: list[str] | None = None) -> dict:
    channels = [c for c in (channels or ["coupang"]) if c != "smartstore"]  # 2026-06-08 네이버 스마트스토어 신규등록 중지(계정한도)

    with get_db() as conn:
        product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        raise ValueError(f"product {product_id} 없음")
    if not product["ai_processed_at"]:
        raise ValueError(f"product {product_id}: AI 처리 미완료")
    if product["status"] == "archived":
        raise ValueError(f"product {product_id}: archived 상태 (재발송 제외)")

    cost_usd = product["cost_usd"]
    if cost_usd is None or cost_usd == "" or float(cost_usd) <= 0:
        # 2026-05-19 사고 직전 가드 — cost=0/None product 가 sourcing 우회로 들어와도 등록 차단
        raise ValueError(f"product {product_id}: cost_usd 비정상 ({cost_usd})")

    # 2026-05-19: safety_filter (Tier 1~6) 등록 직전 backup 가드 — promote 단계 우회 케이스 대비.
    sf_match = safety_filter.is_banned_diet_product(
        product["title_en"] or "", product["title_ko"] or "",
    )
    if sf_match:
        clean_policy.log_violation(
            stage='channel_listing', violation_type='safety_filter',
            action_taken='blocked', matched_keyword=sf_match,
            product_id=product_id, original_text=product["title_ko"] or product["title_en"],
            notes=f'send_to_channels backup 가드: {sf_match}',
        )
        raise ValueError(
            f"product {product_id}: safety_filter 위반 ({sf_match}) — 등록 차단"
        )

    # B 안: 쿠팡 자동매칭 위임 여부 사전 판정 — primary 키워드의 keyword_category_map.source 확인
    # source='ai_soft' (50 ≤ score < 70) 면 coupang_auto_matched=1 마킹
    coupang_auto = 0
    with get_db() as conn:
        kw_row = conn.execute(
            """SELECT m.source FROM product_keywords pk
               JOIN keyword_category_map m ON m.keyword = pk.keyword
               WHERE pk.product_id=? AND pk.is_primary=1
               LIMIT 1""",
            (product_id,),
        ).fetchone()
    if kw_row and kw_row["source"] == "ai_soft":
        coupang_auto = 1

    # 2026-05-15 fix: products.category_path 가 비어있거나 비숫자면 keyword cache fallback.
    # 네이버 카테고리 ID 는 6~12자리 숫자, 비숫자면 smartstore_lister.build_payload 가 reject 함.
    # 동시에 coupang_category_code 도 cache 에서 채워서 쿠팡 자동매칭 대신 우리 시스템 매핑 사용.
    naver_cat = product["category_path"] or ""
    cu_cat_code: int | None = None
    if not (naver_cat and str(naver_cat).isdigit() and 6 <= len(str(naver_cat)) <= 12):
        fb_naver, fb_cu = _resolve_categories_via_cache(product_id)
        if fb_naver:
            naver_cat = fb_naver
            # products.category_path 도 backfill — 이후 detail re-run / build_payload 호출 시 재계산 불요
            with get_db() as conn:
                conn.execute(
                    "UPDATE products SET category_path=? "
                    "WHERE id=? AND (category_path IS NULL OR category_path='')",
                    (naver_cat, product_id),
                )
            logger.info(
                f"[channel-listing] product {product_id} naver category fallback: {naver_cat}"
            )
        if fb_cu:
            cu_cat_code = int(fb_cu)
            logger.info(
                f"[channel-listing] product {product_id} coupang category fallback: {cu_cat_code}"
            )

    # v37: 신규 listing 은 Forwarder default — 누락 비용(안전마진/CS/return) +
    # LBS 무게 요금(또는 fallback) 반영해 사전 정확화. Amazon 직배송비는 0 (이중 차감 방지).
    extras = _load_default_forwarder_extras()
    fw_usd = forwarder_shipping_usd(product["weight_g"])

    results = {}
    skipped = {}
    for ch in channels:
        if _is_archived_failure(product_id, ch):
            skipped[ch] = "archived_as_upload_failure"
            continue

        # ★가격 엔진 v2 — 할인가(landed) cost basis + 새 수수료/반푼 + floor + sanity (등록 시 반영)
        from backend.purchase.services.price_engine import initial_price_by_id
        pricing = initial_price_by_id(product_id, channel=ch)
        # 쿠팡 채널만 auto_matched 마킹 적용 (다른 채널은 0)
        ch_auto = coupang_auto if ch == "coupang" else 0
        # coupang_category_code 도 쿠팡 채널에서만 의미 있음. cache fallback 으로 채워졌을 경우 사용.
        ch_cu_code = cu_cat_code if ch == "coupang" else None

        with get_db() as conn:
            conn.execute(
                # ★2026-08-03: acct_key 포함 3열 유일키로 변경(계정별 행 분리).
                #   pending 단계는 계정 미정이라 acct_key='' 로 두고, 실등록 시 계정값으로 갱신된다.
                """INSERT INTO listings_pa
                   (product_id, channel, status, sale_krw, cost_krw_snapshot,
                    fee_rate, net_margin_krw, category_mapped, coupang_auto_matched,
                    coupang_category_code, acct_key)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, '')
                   ON CONFLICT(product_id, channel, acct_key) DO UPDATE SET
                    status=CASE
                      WHEN listings_pa.channel_product_id IS NOT NULL
                           AND listings_pa.channel_product_id != ''
                        THEN listings_pa.status
                      ELSE 'pending'
                    END,
                    sale_krw=excluded.sale_krw,
                    cost_krw_snapshot=excluded.cost_krw_snapshot,
                    fee_rate=excluded.fee_rate, net_margin_krw=excluded.net_margin_krw,
                    category_mapped=excluded.category_mapped,
                    coupang_auto_matched=excluded.coupang_auto_matched,
                    coupang_category_code=COALESCE(excluded.coupang_category_code, listings_pa.coupang_category_code),
                    last_synced_at=CURRENT_TIMESTAMP""",
                (product_id, ch,
                 pricing["sale_krw"], pricing["cost_krw"], pricing["fee_rate"],
                 pricing["net_margin_krw"], naver_cat,
                 ch_auto, ch_cu_code),
            )

        results[ch] = {
            "sale_krw": pricing["sale_krw"],
            "cost_krw": pricing["cost_krw"],
            "fee_rate": pricing["fee_rate"],
            "net_margin_krw": pricing["net_margin_krw"],
        }

    return {"product_id": product_id, "channels": results, "skipped": skipped}
