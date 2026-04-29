"""
test_group_register.py — Ailun Screen Protector 16-option group register PoC.

대상 그룹: parent B0GY4621HT (Ailun glass screen protector)
범위: iPhone 14~17 + Air = 16 child (전체 27 중 옛 모델 11 제외)
master: B0FDQH511S (iPhone 17 Pro Max — 사용자 지정, 최신/최고가)

사용법:
  cd /home/ubuntu/CharisG-Platform/charisg-platform
  .venv/bin/python -m backend.purchase.scripts.test_group_register --stage 1
  .venv/bin/python -m backend.purchase.scripts.test_group_register --stage 2
  .venv/bin/python -m backend.purchase.scripts.test_group_register --stage 3
  .venv/bin/python -m backend.purchase.scripts.test_group_register --stage 3 --register

선행조건: variation_groups 에 PARENT_ASIN row 가 있어야 함
  → 없으면 먼저 test_group_pipeline B0FDQH511S 실행
"""
import argparse
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(get_db)


PARENT_ASIN = "B0GY4621HT"
MASTER_ASIN = "B0FDQH511S"
COUPANG_CATEGORY_CODE = 62618  # 가전/디지털>...>케이스/보호필름>보호필름>전면보호
NAVER_CATEGORY_ID = "50004595"  # 디지털/가전>휴대폰액세서리>휴대폰보호필름>액정보호필름
GROUP_TITLE_KO = "Ailun 아이폰 강화유리 화면보호필름 + 카메라렌즈 보호필름 3+3 세트 (14/15/16/17/Air)"
SCOPE_16 = [
    # master 첫 번째 (iPhone 17 Pro Max — auto_split 첫 child 가 master 로 사용됨)
    "B0FDQH511S",
    # iPhone 14
    "B0B76D5ZN5", "B0B76BB2GJ", "B0B768WHRS", "B0B76KJXWV",
    # iPhone 15
    "B0CCYN42DL", "B0CCYR15GT", "B0CCYNY9XY", "B0CCYNV9PC",
    # iPhone 16
    "B0D9LK557W", "B0D9LKTZ9M", "B0D9LJPKF5", "B0D9LJPWX9",
    # iPhone 17 (나머지) + Air
    "B0FDQLSR6Q", "B0FDQG97BP", "B0FDQK7K8D",
]


# ── 라벨 정규화 ────────────────────────────────────────
_IPHONE_RE = re.compile(
    r"for\s+(iPhone\s+(?:1[0-9]|Air)(?:\s+Pro\s+Max|\s+Pro|\s+Plus|\s+Mini)?)\s*\[",
    re.IGNORECASE,
)


def extract_model_name(title: str) -> str | None:
    if not title:
        return None
    m = _IPHONE_RE.search(title)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return None


_SERIES_RE = re.compile(r"^(iPhone\s+(?:1[0-9]|Air))", re.IGNORECASE)


def extract_series(label: str | None) -> str:
    if not label:
        return ""
    m = _SERIES_RE.match(label)
    return m.group(1) if m else ""


def normalize_size_label(title: str | None, current: str | None) -> str:
    """size_label 이 모델명을 포함하면 그대로, 없으면 title 에서 보강.

    예:
      current="iPhone 14 Plus-6.7 inch" → 그대로
      current="6.9 Inch", title="...for iPhone 17 Pro Max [6.9 inch]..." → "iPhone 17 Pro Max-6.9 Inch"
    """
    if current and "iPhone" in current:
        return current
    model = extract_model_name(title or "")
    if model and current:
        return f"{model}-{current}"
    return current or "(unknown)"


# ── facts.size_label 정규화 patch (Stage 3 임시) ──────────
def patch_facts_size_label(asins: list[str]) -> dict:
    """SCOPE_16 의 sp_api_facts_json.size_label 을 normalize_size_label 로 보정.
    iPhone 17 시리즈 4개의 raw "6.3 Inch" → "iPhone 17 Pro-6.3 Inch" 같이 통일.
    반환: {asin: original_size_label} 백업 dict (None 가능)
    """
    placeholders = ",".join("?" * len(asins))
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT asin, title_en, sp_api_facts_json FROM products
                WHERE asin IN ({placeholders})""",
            asins,
        ).fetchall()
    backup: dict = {}
    patched = 0
    for r in rows:
        try:
            facts = json.loads(r["sp_api_facts_json"] or "{}")
        except json.JSONDecodeError:
            continue
        original = facts.get("size_label")
        normalized = normalize_size_label(r["title_en"], original)
        if normalized == original:
            continue
        facts["size_label"] = normalized
        backup[r["asin"]] = original
        with get_db() as conn:
            conn.execute(
                "UPDATE products SET sp_api_facts_json=? WHERE asin=?",
                (json.dumps(facts, ensure_ascii=False), r["asin"]),
            )
        patched += 1
        print(f"    patch {r['asin']}: '{original}' → '{normalized}'")
    print(f"  facts.size_label patched: {patched}/{len(rows)}")
    return backup


def patch_facts_color(asins: list[str], default_color: str) -> dict:
    """SCOPE_16 의 sp_api_facts_json.color 가 비어있거나 default 와 다르면 default 로 채움.
    쿠팡 mandatory 색상 attribute 누락 reject 방지용.

    반환: {asin: original_color} 백업 dict
    """
    placeholders = ",".join("?" * len(asins))
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT asin, sp_api_facts_json FROM products
                WHERE asin IN ({placeholders})""",
            asins,
        ).fetchall()
    backup: dict = {}
    patched_missing = patched_normalize = 0
    for r in rows:
        try:
            facts = json.loads(r["sp_api_facts_json"] or "{}")
        except json.JSONDecodeError:
            continue
        original = facts.get("color")
        if original == default_color:
            continue
        backup[r["asin"]] = original
        facts["color"] = default_color
        with get_db() as conn:
            conn.execute(
                "UPDATE products SET sp_api_facts_json=? WHERE asin=?",
                (json.dumps(facts, ensure_ascii=False), r["asin"]),
            )
        if not original:
            patched_missing += 1
            print(f"    fill {r['asin']}: (missing) → '{default_color}'")
        else:
            patched_normalize += 1
            print(f"    norm {r['asin']}: '{original}' → '{default_color}'")
    print(f"  facts.color: missing fill {patched_missing}, normalize {patched_normalize}")
    return backup


def restore_facts_color(backup: dict) -> None:
    for asin, original in backup.items():
        with get_db() as conn:
            row = conn.execute(
                "SELECT sp_api_facts_json FROM products WHERE asin=?", (asin,),
            ).fetchone()
        try:
            facts = json.loads(row["sp_api_facts_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if original is None:
            facts.pop("color", None)
        else:
            facts["color"] = original
        with get_db() as conn:
            conn.execute(
                "UPDATE products SET sp_api_facts_json=? WHERE asin=?",
                (json.dumps(facts, ensure_ascii=False), asin),
            )


def restore_facts_size_label(backup: dict) -> None:
    for asin, original in backup.items():
        with get_db() as conn:
            row = conn.execute(
                "SELECT sp_api_facts_json FROM products WHERE asin=?", (asin,),
            ).fetchone()
        try:
            facts = json.loads(row["sp_api_facts_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if original is None:
            facts.pop("size_label", None)
        else:
            facts["size_label"] = original
        with get_db() as conn:
            conn.execute(
                "UPDATE products SET sp_api_facts_json=? WHERE asin=?",
                (json.dumps(facts, ensure_ascii=False), asin),
            )


# ── Cost refill (A: API 재시도 → B: 같은 시리즈 fallback) ──
def refill_missing_costs(asins: list[str], wait_sec: int = 60) -> dict:
    import time as _time
    from backend.purchase.services.group_lister import _get_buybox_or_lowest_price

    placeholders = ",".join("?" * len(asins))
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT asin, option_label FROM products
                WHERE asin IN ({placeholders}) AND (cost_usd IS NULL OR cost_usd = 0)""",
            asins,
        ).fetchall()
    missing = [(r["asin"], r["option_label"]) for r in rows]
    if not missing:
        print(f"\n  cost_usd 누락 없음 — refill skip")
        return {"missing": 0, "ok": 0, "fallback": 0, "still_missing": 0}

    print(f"\n  cost_usd 누락 {len(missing)} 건. quota 회복 대기 {wait_sec}s …")
    _time.sleep(wait_sec)

    ok = fallback = still_missing = 0
    for asin, label in missing:
        price = _get_buybox_or_lowest_price(asin)
        if price and price > 0:
            with get_db() as conn:
                conn.execute("UPDATE products SET cost_usd=? WHERE asin=?", (price, asin))
            ok += 1
            print(f"    A) {asin}: ${price:.2f} (BuyBox 재시도 성공)")
        else:
            series = extract_series(label)
            fb_price = None
            if series:
                with get_db() as conn:
                    fb = conn.execute(
                        f"""SELECT cost_usd FROM products
                            WHERE asin IN ({placeholders})
                              AND option_label LIKE ?
                              AND cost_usd > 0
                            ORDER BY cost_usd DESC LIMIT 1""",
                        (*asins, f"{series}%"),
                    ).fetchone()
                if fb:
                    fb_price = float(fb["cost_usd"])
            if fb_price:
                with get_db() as conn:
                    conn.execute("UPDATE products SET cost_usd=? WHERE asin=?", (fb_price, asin))
                fallback += 1
                print(f"    B) {asin}: ${fb_price:.2f} (fallback: {series} 시리즈 최대값)")
            else:
                still_missing += 1
                print(f"    ✗ {asin}: 재시도 + fallback 모두 실패")
        _time.sleep(1.1)
    return {"missing": len(missing), "ok": ok, "fallback": fallback, "still_missing": still_missing}


# ── Stage 1 ────────────────────────────────────────────
def stage1() -> int:
    print()
    print("=== Stage 1: 16 child facts + cost INSERT ===")
    print()

    # 1) variation_groups 확인
    with get_db() as conn:
        vg = conn.execute(
            "SELECT * FROM variation_groups WHERE parent_asin=?", (PARENT_ASIN,),
        ).fetchone()
    if not vg:
        print(f"[FAIL] variation_groups 에 {PARENT_ASIN} 없음.")
        print(f"       먼저: python -m backend.purchase.scripts.test_group_pipeline B0FDQH511S")
        return 1
    print(f"  variation_groups: child_count={vg['child_count']}, theme={vg['variation_theme']}")

    # 2) child INSERT (facts + BuyBox cost 동시 호출)
    from backend.purchase.services.group_lister import fetch_and_insert_children
    print(f"\n  → fetch_and_insert_children({PARENT_ASIN}) … 27 ASIN, ~30s")
    result = fetch_and_insert_children(PARENT_ASIN)
    print(f"  결과: {result}")

    # 3) variation_groups 에 master 지정
    with get_db() as conn:
        master_row = conn.execute(
            "SELECT id, title_en, brand FROM products WHERE asin=?", (MASTER_ASIN,),
        ).fetchone()
        if not master_row:
            print(f"\n[FAIL] master {MASTER_ASIN} products 에 INSERT 안 됨")
            return 1
        base_name = _IPHONE_RE.sub("for ", master_row["title_en"] or "")
        conn.execute(
            """UPDATE variation_groups
               SET master_asin=?, brand=?, base_name_en=?,
                   ingestion_status='children_loaded',
                   children_loaded_at=datetime('now')
               WHERE parent_asin=?""",
            (MASTER_ASIN, master_row["brand"], base_name, PARENT_ASIN),
        )
        conn.execute(
            "UPDATE products SET is_group_master=1 WHERE asin=?",
            (MASTER_ASIN,),
        )

    # 4) 16 child 의 group_master_asin + 라벨 정규화
    placeholders = ",".join("?" * len(SCOPE_16))
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT asin, title_en, sp_api_facts_json
                FROM products WHERE asin IN ({placeholders})""",
            SCOPE_16,
        ).fetchall()
        for r in rows:
            try:
                facts = json.loads(r["sp_api_facts_json"] or "{}")
            except json.JSONDecodeError:
                facts = {}
            current = facts.get("size_label") or facts.get("size_attr")
            label = normalize_size_label(r["title_en"], current)
            conn.execute(
                """UPDATE products
                   SET group_master_asin=?, option_label=?,
                       option_dimensions_json=?
                   WHERE asin=?""",
                (MASTER_ASIN, label, '["model_number"]', r["asin"]),
            )

    # 5) 결과 출력
    print()
    print("=== SCOPE_16 상태 ===")
    print()
    print(f"{'ASIN':<12} {'cost_usd':<10} {'option_label':<32} {'title':<55}")
    print("-" * 112)
    with get_db() as conn:
        order_case = " ".join(f"WHEN '{a}' THEN {i}" for i, a in enumerate(SCOPE_16))
        rows = conn.execute(
            f"""SELECT asin, cost_usd, option_label, title_en
                FROM products WHERE asin IN ({placeholders})
                ORDER BY CASE asin {order_case} END""",
            SCOPE_16,
        ).fetchall()
    no_cost = 0
    for r in rows:
        cost = f"${r['cost_usd']:.2f}" if r["cost_usd"] else "(none)"
        if not r["cost_usd"]:
            no_cost += 1
        title = (r["title_en"] or "")[:55]
        label = (r["option_label"] or "-")[:32]
        print(f"{r['asin']:<12} {cost:<10} {label:<32} {title}")
    print()
    print(f"  found: {len(rows)}/16, cost_usd 없음: {no_cost}/16")

    # 6) 누락된 cost 재시도 (A: API 재호출 → B: 같은 시리즈 fallback)
    refill = refill_missing_costs(SCOPE_16, wait_sec=60)
    print(f"\n  refill: {refill}")

    # 7) 최종 상태 출력
    if refill["missing"] > 0:
        print()
        print("=== refill 후 SCOPE_16 ===")
        with get_db() as conn:
            rows = conn.execute(
                f"""SELECT asin, cost_usd, option_label FROM products
                    WHERE asin IN ({placeholders})
                    ORDER BY CASE asin {order_case} END""",
                SCOPE_16,
            ).fetchall()
        final_missing = 0
        for r in rows:
            cost = f"${r['cost_usd']:.2f}" if r["cost_usd"] else "(none)"
            if not r["cost_usd"]:
                final_missing += 1
            print(f"  {r['asin']:<12} {cost:<10} {r['option_label']}")
        print(f"\n  최종 cost 누락: {final_missing}/16")
    print(f"\n  → 결과 OK 면: --stage 2")
    return 0


# ── Stage 2: 카테고리 + 옵션별 마진 가격 + detail/이미지 ──
def stage2() -> int:
    import asyncio

    print()
    print("=== Stage 2: 카테고리 + 옵션별 가격 + detail/이미지 ===")
    print()

    placeholders = ",".join("?" * len(SCOPE_16))

    # 1) master row
    with get_db() as conn:
        master = conn.execute(
            """SELECT id, asin, title_en, title_ko, brand, category_path,
                      sale_price_krw, cost_usd, images_json
               FROM products WHERE asin=?""",
            (MASTER_ASIN,),
        ).fetchone()
    if not master:
        print(f"[FAIL] master {MASTER_ASIN} 없음")
        return 1
    master_id = master["id"]
    print(f"  master id={master_id}, asin={master['asin']}, cost=${master['cost_usd']:.2f}")

    # 2) 쿠팡 카테고리 코드 → variation_groups.category_path 에 저장
    #    (find_category_with_gemini 는 영문 title 에서 "탁상용액자" 같은 오답 → 수동 지정)
    with get_db() as conn:
        cat_row = conn.execute(
            "SELECT code, name, path FROM coupang_categories WHERE code=?",
            (COUPANG_CATEGORY_CODE,),
        ).fetchone()
        if not cat_row:
            print(f"  [FAIL] coupang_categories 에 {COUPANG_CATEGORY_CODE} 없음")
            return 1
        conn.execute(
            "UPDATE variation_groups SET category_path=? WHERE parent_asin=?",
            (str(COUPANG_CATEGORY_CODE), PARENT_ASIN),
        )
    print(f"  쿠팡 카테고리 (variation_groups.category_path):")
    print(f"    code={cat_row['code']}, name={cat_row['name']}")
    print(f"    path={cat_row['path']}")

    # 3) 16 child 옵션별 sale_price_krw (쿠팡 기준)
    from backend.purchase.services.pricing_service_pa import calculate_sale_krw
    print()
    print(f"=== 옵션별 가격 (쿠팡, 마진 35%) ===")
    print(f"  {'ASIN':<12} {'cost':<8} {'sale_krw':<12} {'margin':<10} {'option_label'}")
    print(f"  {'-' * 80}")
    order_case = " ".join(f"WHEN '{a}' THEN {i}" for i, a in enumerate(SCOPE_16))
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT id, asin, cost_usd, option_label, sale_price_krw FROM products
                WHERE asin IN ({placeholders})
                ORDER BY CASE asin {order_case} END""",
            SCOPE_16,
        ).fetchall()
    priced = 0
    for r in rows:
        if not r["cost_usd"] or float(r["cost_usd"]) <= 0:
            print(f"  {r['asin']:<12} -        (cost 없음)")
            continue
        try:
            result = calculate_sale_krw(
                cost_usd=float(r["cost_usd"]),
                channel="coupang",
            )
            sale_krw = int(result["sale_krw"])
            margin_krw = int(result["net_margin_krw"])
            with get_db() as conn:
                conn.execute(
                    "UPDATE products SET sale_price_krw=? WHERE id=?",
                    (sale_krw, r["id"]),
                )
            priced += 1
            print(
                f"  {r['asin']:<12} ${r['cost_usd']:<7.2f} ₩{sale_krw:<11,} ₩{margin_krw:<9,} {r['option_label']}"
            )
        except Exception as e:
            print(f"  {r['asin']:<12} [FAIL] {e}")
    print(f"\n  priced: {priced}/16")

    # 4) master detail_pages — backend_shared.detail_page_service 가 존재하지
    #    않는 'templates' 테이블 참조하는 버그가 있어 임시로 직접 INSERT.
    print()
    with get_db() as conn:
        has_detail = conn.execute(
            """SELECT 1 FROM detail_pages
               WHERE product_id=? AND html_content IS NOT NULL AND html_content != ''
               LIMIT 1""",
            (master_id,),
        ).fetchone()
    if has_detail:
        print(f"  [SKIP] detail_pages 이미 있음 (master id={master_id})")
    else:
        with get_db() as conn:
            master_full = conn.execute(
                "SELECT title_en, title_ko, brand, sp_api_facts_json FROM products WHERE id=?",
                (master_id,),
            ).fetchone()
            cache_rows = conn.execute(
                """SELECT public_url FROM image_cache
                   WHERE product_id=? ORDER BY image_idx""",
                (master_id,),
            ).fetchall()
        try:
            facts = json.loads(master_full["sp_api_facts_json"] or "{}")
        except json.JSONDecodeError:
            facts = {}
        bullets = facts.get("bullet_points") or []
        title_for_html = master_full["title_ko"] or master_full["title_en"] or ""

        html_parts = [f"<h2>{title_for_html}</h2>"]
        for r in cache_rows:
            html_parts.append(f'<p><img src="{r["public_url"]}" style="max-width:100%;" /></p>')
        if bullets:
            html_parts.append("<ul>")
            for b in bullets:
                html_parts.append(f"<li>{b}</li>")
            html_parts.append("</ul>")
        html_parts.append(
            "<p>본 상품은 해외구매대행 상품으로, 통관 및 관부가세는 별도입니다.</p>"
        )
        html = "\n".join(html_parts)

        with get_db() as conn:
            conn.execute(
                """INSERT INTO detail_pages
                   (product_id, template_id, sections, html_content, status,
                    generated_by, market, platform)
                   VALUES (?, NULL, '[]', ?, 'draft', 'manual_poc', 'KR', 'coupang')""",
                (master_id, html),
            )
        print(f"  detail_pages 임시 INSERT (master id={master_id}, {len(html):,} bytes)")

    # 5) master 이미지 다운로드 → image_cache (모든 child 공유)
    print()
    with get_db() as conn:
        cache = conn.execute(
            "SELECT COUNT(*) c FROM image_cache WHERE product_id=?", (master_id,),
        ).fetchone()
    if cache and cache["c"] > 0:
        print(f"  [SKIP] image_cache 이미 있음: {cache['c']}장 (master id={master_id})")
    else:
        try:
            from backend.purchase.services.image_downloader import download_product_images
            with get_db() as conn:
                mp = conn.execute(
                    "SELECT images_json FROM products WHERE id=?", (master_id,),
                ).fetchone()
            if mp and mp["images_json"]:
                result = asyncio.run(
                    download_product_images(master_id, mp["images_json"])
                )
                print(f"  이미지 다운로드: {result}")
            else:
                print(f"  [WARN] master images_json 없음")
        except Exception as e:
            print(f"  [WARN] 이미지 다운로드 실패: {e}")

    # 6) 최종 검증
    print()
    print("=== Stage 2 결과 ===")
    with get_db() as conn:
        m2 = conn.execute(
            "SELECT sale_price_krw FROM products WHERE id=?", (master_id,),
        ).fetchone()
        vg2 = conn.execute(
            "SELECT category_path FROM variation_groups WHERE parent_asin=?",
            (PARENT_ASIN,),
        ).fetchone()
        priced_count = conn.execute(
            f"""SELECT COUNT(*) c FROM products
                WHERE asin IN ({placeholders}) AND sale_price_krw > 0""",
            SCOPE_16,
        ).fetchone()["c"]
        cache_count = conn.execute(
            "SELECT COUNT(*) c FROM image_cache WHERE product_id=?", (master_id,),
        ).fetchone()["c"]
        detail_ok = conn.execute(
            """SELECT 1 FROM detail_pages
               WHERE product_id=? AND html_content IS NOT NULL AND html_content != ''
               LIMIT 1""",
            (master_id,),
        ).fetchone()
    print(f"  variation_groups.category_path : {vg2['category_path']} (쿠팡 코드)")
    print(f"  master.sale_price_krw          : {m2['sale_price_krw']}")
    print(f"  child priced (>0)              : {priced_count}/16")
    print(f"  master image_cache             : {cache_count}장")
    print(f"  master detail_page             : {'OK' if detail_ok else 'NONE'}")
    all_ok = (
        priced_count == 16
        and vg2["category_path"]
        and detail_ok
        and cache_count > 0
    )
    if all_ok:
        print(f"\n  → 모두 OK. --stage 3 진행 가능")
    else:
        print(f"\n  ⚠ 누락 항목 있음")
    return 0


def stage3(register: bool) -> int:
    """쿠팡 multi-option 등록.

    register=False: dry_run 페이로드 출력
    register=True : dry_run 출력 + 3초 후 자동 실등록
    """
    import time as _time

    print()
    print(f"=== Stage 3: 쿠팡 multi-option 등록 (register={register}) ===")
    print()

    # 1) variation_groups 검증
    with get_db() as conn:
        vg = conn.execute(
            "SELECT * FROM variation_groups WHERE parent_asin=?", (PARENT_ASIN,),
        ).fetchone()
    if not vg or not vg["category_path"]:
        print(f"[FAIL] variation_groups 누락 또는 카테고리 없음. Stage 2 먼저.")
        return 1
    if str(vg["category_path"]) != str(COUPANG_CATEGORY_CODE):
        print(f"[WARN] variation_groups.category_path={vg['category_path']} ≠ {COUPANG_CATEGORY_CODE}")

    # 2) master 의 products id 조회
    with get_db() as conn:
        master = conn.execute(
            "SELECT id, sale_price_krw, cost_usd FROM products WHERE asin=?",
            (MASTER_ASIN,),
        ).fetchone()
    if not master:
        print(f"[FAIL] master {MASTER_ASIN} products 없음")
        return 1
    master_id = master["id"]

    # 3) child_asins_json + base_name_ko 백업 + 한국어 제목 INSERT + SCOPE_16 교체
    original_child_asins = vg["child_asins_json"]
    original_base_name_ko = vg["base_name_ko"]
    with get_db() as conn:
        conn.execute(
            """UPDATE variation_groups
               SET child_asins_json=?, base_name_ko=?
               WHERE parent_asin=?""",
            (json.dumps(SCOPE_16), GROUP_TITLE_KO, PARENT_ASIN),
        )
    print(f"  child_asins_json 임시 교체: 27 → {len(SCOPE_16)}")
    print(f"  base_name_ko 설정: {GROUP_TITLE_KO}")

    # 3.5) Option A: facts.size_label 정규화 (iPhone 17 시리즈 4개 보정 → 사이즈 attribute unique)
    print(f"\n  --- Option A: facts.size_label patch ---")
    facts_backup = patch_facts_size_label(SCOPE_16)

    # 3.6) Option B: facts.color 통일 (mandatory 색상 누락 reject 해소 + 일관성)
    print(f"\n  --- Option B: facts.color patch (default = 'Transparent') ---")
    color_backup = patch_facts_color(SCOPE_16, default_color="Transparent")

    # 4) listings_pa placeholder INSERT (master, coupang, coupang_category_code=62618)
    with get_db() as conn:
        existing_lp = conn.execute(
            "SELECT id, coupang_category_code FROM listings_pa WHERE product_id=? AND channel='coupang'",
            (master_id,),
        ).fetchone()
        if existing_lp:
            if existing_lp["coupang_category_code"] != COUPANG_CATEGORY_CODE:
                conn.execute(
                    "UPDATE listings_pa SET coupang_category_code=? WHERE id=?",
                    (COUPANG_CATEGORY_CODE, existing_lp["id"]),
                )
                print(f"  listings_pa coupang 기존 행 카테고리 UPDATE → {COUPANG_CATEGORY_CODE}")
            else:
                print(f"  listings_pa coupang placeholder 이미 있음 (cat={COUPANG_CATEGORY_CODE})")
        else:
            conn.execute(
                """INSERT INTO listings_pa
                   (product_id, channel, status, coupang_category_code)
                   VALUES (?, 'coupang', 'pending', ?)""",
                (master_id, COUPANG_CATEGORY_CODE),
            )
            print(f"  listings_pa coupang placeholder INSERT (cat={COUPANG_CATEGORY_CODE})")

    try:
        from backend.purchase.services.group_lister import register_new_group_listing
        from backend.purchase.services.group_lister import build_coupang_payload
        from backend.purchase.services.variation import (
            load_group, auto_split, calculate_group_pricing,
        )

        # 4.5) 페이로드 직접 빌드 → items 차원값 dump
        print(f"\n--- PAYLOAD INSPECT ---")
        gp = load_group(PARENT_ASIN)
        splits = auto_split(gp, "coupang")
        pricing = calculate_group_pricing(gp, "coupang")
        by_asin = {p["child_asin"]: p for p in pricing}
        for sp in splits:
            opt_asins = [o.get("asin") for o in sp.get("options") or []]
            sp_pricing = [by_asin[a] for a in opt_asins if a in by_asin]
            payload = build_coupang_payload(gp, sp, sp_pricing)
            if not payload:
                print(f"  [SKIP split] payload build fail")
                continue
            items = payload.get("items", [])
            print(f"  sellerProductName: {payload.get('sellerProductName')}")
            print(f"  displayCategoryCode: {payload.get('displayCategoryCode')}")
            print(f"  items count: {len(items)}")
            print(f"  {'#':<3} {'itemName':<25} {'사이즈':<28} {'색상':<14} {'스타일':<12} salePrice")
            print(f"  {'-' * 100}")
            for i, item in enumerate(items):
                attrs = {a.get("attributeTypeName"): a.get("attributeValueName")
                         for a in item.get("attributes", [])}
                print(
                    f"  {i:<3} "
                    f"{(item.get('itemName') or '-')[:25]:<25} "
                    f"{(attrs.get('사이즈') or '-')[:28]:<28} "
                    f"{(attrs.get('색상') or '-')[:14]:<14} "
                    f"{(attrs.get('스타일') or '-')[:12]:<12} "
                    f"{item.get('salePrice')}"
                )
            # 중복 검출
            sigs = [
                (item.get("itemName"),
                 tuple(sorted((a.get("attributeTypeName"), a.get("attributeValueName"))
                              for a in item.get("attributes", []))))
                for item in items
            ]
            from collections import Counter
            cnt = Counter(sigs)
            dups = [s for s, c in cnt.items() if c > 1]
            if dups:
                print(f"\n  ⚠ 페이로드 내 중복 attribute 조합 {len(dups)} 종류:")
                for s in dups:
                    print(f"     {s}")

        # 5) dry_run 호출
        print(f"\n--- DRY RUN ---")
        result = register_new_group_listing(
            parent_asin=PARENT_ASIN,
            channels=["coupang"],
            dry_run=True,
        )
        coupang_results = result.get("channels", {}).get("coupang", [])
        print(json.dumps(result, indent=2, ensure_ascii=False)[:3000])
        if not coupang_results:
            print(f"\n[FAIL] coupang dry_run 결과 없음")
            return 1
        # split 별 옵션 개수 출력
        for r in coupang_results:
            print(f"  split #{r.get('split_index')}: {r.get('status')}, "
                  f"options={r.get('options_count')}, master={r.get('master_child_asin')}")

        if not register:
            print(f"\n  → dry_run OK. 실등록은: --stage 3 --register")
            return 0

        # 6) 자동 실등록
        print(f"\n  3초 후 실등록 (Ctrl+C 로 중단 가능)…")
        for i in (3, 2, 1):
            print(f"    {i}…")
            _time.sleep(1)

        print(f"\n--- 실등록 ---")
        result2 = register_new_group_listing(
            parent_asin=PARENT_ASIN,
            channels=["coupang"],
            dry_run=False,
        )
        print(json.dumps(result2, indent=2, ensure_ascii=False)[:3000])
        cp_results = result2.get("channels", {}).get("coupang", [])
        for r in cp_results:
            print(f"  split #{r.get('split_index')}: {r.get('status')}, "
                  f"channel_product_id={r.get('channel_product_id')}")
            if r.get("error"):
                print(f"    error: {r['error']}")
        return 0
    finally:
        # 7) facts.size_label + facts.color + child_asins_json + base_name_ko 원복
        restore_facts_size_label(facts_backup)
        restore_facts_color(color_backup)
        with get_db() as conn:
            conn.execute(
                """UPDATE variation_groups
                   SET child_asins_json=?, base_name_ko=?
                   WHERE parent_asin=?""",
                (original_child_asins, original_base_name_ko, PARENT_ASIN),
            )
        print(f"\n  facts + child_asins_json + base_name_ko 원복")


def stage4(register: bool) -> int:
    """네이버 스마트스토어 multi-option 등록.

    선행: Stage 1~3 (쿠팡 등록까지) 완료 상태 가정.
    products 의 cost_usd / facts / option_label / sale_price_krw 그대로 사용.
    카테고리만 네이버용 (50004595) 으로 변경.
    """
    import time as _time

    print()
    print(f"=== Stage 4: 네이버 스마트스토어 multi-option 등록 (register={register}) ===")
    print()

    # 1) variation_groups 검증
    with get_db() as conn:
        vg = conn.execute(
            "SELECT * FROM variation_groups WHERE parent_asin=?", (PARENT_ASIN,),
        ).fetchone()
    if not vg:
        print(f"[FAIL] variation_groups 없음")
        return 1

    # 2) master products.category_path = NAVER_CATEGORY_ID
    with get_db() as conn:
        master = conn.execute(
            "SELECT id, category_path FROM products WHERE asin=?", (MASTER_ASIN,),
        ).fetchone()
    if not master:
        print(f"[FAIL] master {MASTER_ASIN} 없음")
        return 1
    master_id = master["id"]
    original_category = master["category_path"]
    with get_db() as conn:
        conn.execute(
            "UPDATE products SET category_path=? WHERE id=?",
            (NAVER_CATEGORY_ID, master_id),
        )
    print(f"  master.category_path: {original_category} → {NAVER_CATEGORY_ID}")

    # 3) child_asins_json + base_name_ko 임시 교체
    original_child_asins = vg["child_asins_json"]
    original_base_name_ko = vg["base_name_ko"]
    with get_db() as conn:
        conn.execute(
            """UPDATE variation_groups
               SET child_asins_json=?, base_name_ko=?
               WHERE parent_asin=?""",
            (json.dumps(SCOPE_16), GROUP_TITLE_KO, PARENT_ASIN),
        )
    print(f"  child_asins_json: 27 → 16, base_name_ko 설정")

    # 4) Option A + B 동일 적용 (사이즈 unique + 색상 mandatory)
    print(f"\n  --- Option A: facts.size_label patch ---")
    facts_backup = patch_facts_size_label(SCOPE_16)
    print(f"\n  --- Option B: facts.color patch (default = 'Transparent') ---")
    color_backup = patch_facts_color(SCOPE_16, default_color="Transparent")

    try:
        from backend.purchase.services.group_lister import (
            register_new_group_listing, build_smartstore_payload
        )
        from backend.purchase.services.variation import (
            load_group, auto_split, calculate_group_pricing,
        )

        # 5) 페이로드 inspect
        print(f"\n--- PAYLOAD INSPECT (smartstore) ---")
        gp = load_group(PARENT_ASIN)
        splits = auto_split(gp, "smartstore")
        pricing = calculate_group_pricing(gp, "smartstore")
        by_asin = {p["child_asin"]: p for p in pricing}
        for sp in splits:
            opt_asins = [o.get("asin") for o in sp.get("options") or []]
            sp_pricing = [by_asin[a] for a in opt_asins if a in by_asin]
            payload = build_smartstore_payload(gp, sp, sp_pricing)
            if not payload:
                print("  [SKIP] payload build fail")
                continue
            op = payload.get("originProduct") or {}
            print(f"  name: {op.get('name')}")
            print(f"  categoryId: {op.get('categoryId')}")
            print(f"  salePrice: {op.get('salePrice')}")
            opts = (op.get("detailAttribute") or {}).get("optionInfo") or {}
            opt_combs = opts.get("optionCombinations") or []
            print(f"  optionCombinations: {len(opt_combs)}")
            for i, oc in enumerate(opt_combs):
                price = oc.get("price")
                stock = oc.get("stockQuantity")
                names = " / ".join(
                    str(oc.get(f"optionName{j}") or "") for j in (1, 2, 3) if oc.get(f"optionName{j}")
                )
                print(f"    [{i:2}] {names:<50} ₩{price:<7} stock={stock}")

        # 6) dry_run
        print(f"\n--- DRY RUN ---")
        result = register_new_group_listing(
            parent_asin=PARENT_ASIN,
            channels=["smartstore"],
            dry_run=True,
        )
        ss_results = result.get("channels", {}).get("smartstore", [])
        for r in ss_results:
            print(f"  split #{r.get('split_index')}: {r.get('status')}, "
                  f"options={r.get('options_count')}, master={r.get('master_child_asin')}")

        if not register:
            print(f"\n  → dry_run OK. 실등록은: --stage 4 --register")
            return 0

        # 7) 실등록
        print(f"\n  3초 후 실등록 (Ctrl+C 로 중단 가능)…")
        for i in (3, 2, 1):
            print(f"    {i}…")
            _time.sleep(1)

        print(f"\n--- 실등록 ---")
        result2 = register_new_group_listing(
            parent_asin=PARENT_ASIN,
            channels=["smartstore"],
            dry_run=False,
        )
        ss_results = result2.get("channels", {}).get("smartstore", [])
        for r in ss_results:
            print(f"  split #{r.get('split_index')}: {r.get('status')}, "
                  f"channel_product_id={r.get('channel_product_id')}")
            if r.get("error"):
                print(f"    error: {str(r['error'])[:500]}")
        return 0
    finally:
        restore_facts_size_label(facts_backup)
        restore_facts_color(color_backup)
        with get_db() as conn:
            conn.execute(
                """UPDATE variation_groups
                   SET child_asins_json=?, base_name_ko=?
                   WHERE parent_asin=?""",
                (original_child_asins, original_base_name_ko, PARENT_ASIN),
            )
            # category_path 는 다음 등록/수정 시 재참조될 수 있어 NAVER_CATEGORY_ID 그대로 유지
        print(f"\n  facts + child_asins_json + base_name_ko 원복 (category_path 는 유지)")


def stage5_fix_naver(register: bool) -> int:
    """이미 등록된 네이버 상품(13397616592) 의 detailContent + originAreaInfo 갱신.

    1) detail_page_service.generate_detail_page 의 _load_sections templates 버그 우회 (monkey patch)
    2) master 의 detail_pages 새 row INSERT (platform='smartstore', 표준 HTML)
    3) update_product API 호출 → originAreaInfo + detailContent 갱신
    """
    print()
    print(f"=== Stage 5: 네이버 등록 상품 fix (originArea + detail) (register={register}) ===")
    print()

    NAVER_PRODUCT_NO = "13397616592"

    # 1) detail_page_service Fix 2 적용됨 — monkey patch 불필요
    import backend_shared.detail_page_service as _dps
    print("  detail_page_service Fix 2 (templates → detail_templates) 적용 가정")

    # 2) master 의 네이버 CDN 이미지 URL 수집 (등록 시 업로드됨)
    with get_db() as conn:
        master = conn.execute(
            "SELECT id, asin, title_en, brand FROM products WHERE asin=?", (MASTER_ASIN,),
        ).fetchone()
    if not master:
        print(f"[FAIL] master {MASTER_ASIN} 없음")
        return 1
    master_id = master["id"]

    from backend.purchase.services.smartstore_lister import _get_product_images as _ss_get_images
    ss_image_urls = _ss_get_images(master_id) or []
    print(f"  네이버 CDN 이미지: {len(ss_image_urls)}장")
    if not ss_image_urls:
        print(f"  [WARN] 네이버 CDN 이미지 없음 — public_url 로 대체")
        with get_db() as conn:
            cache = conn.execute(
                "SELECT public_url FROM image_cache WHERE product_id=? ORDER BY image_idx",
                (master_id,),
            ).fetchall()
        from backend_shared._config import PUBLIC_BASE_URL
        base = (PUBLIC_BASE_URL or "").rstrip("/")
        ss_image_urls = [f"{base}{r['public_url']}" if base else r["public_url"] for r in cache]
        print(f"  fallback URL: {len(ss_image_urls)}장")

    # 3) master_dict 만들기 (generate_detail_page 가 기대하는 키 매핑)
    master_dict = {
        "id": master_id,
        "asin": master["asin"],
        "product_name": GROUP_TITLE_KO,
        "product_name_kr": GROUP_TITLE_KO,
        "product_name_processed": GROUP_TITLE_KO,
        "description": "",
        "description_kr": "",
        "image_url": ss_image_urls[0] if ss_image_urls else "",
        "images_processed": json.dumps(ss_image_urls),
        "specs": json.dumps({"브랜드": master["brand"], "원산지": "상세페이지 참고", "구성": "화면보호필름 3개 + 카메라렌즈 보호필름 3개"}),
    }

    # 4) generate_detail_page 호출 (Fix 2 적용으로 정상 작동)
    from backend_shared.detail_page_service import generate_detail_page
    try:
        result = generate_detail_page(
            product=master_dict, market="KR", platform="smartstore",
        )
        new_html = result["html"]
        print(f"  표준 HTML 생성: {len(new_html):,} bytes, "
              f"sections={result.get('sections_count')}, detail_id={result.get('detail_page_id')}")
    except Exception as e:
        print(f"  [FAIL] generate_detail_page 실패: {e}")
        return 1

    # 5) dry_run / 실등록
    print(f"\n  detailContent 미리보기 (앞 400자):")
    print(f"  {new_html[:400]}...")

    if not register:
        print(f"\n  → 미리보기 OK 면: --stage 5 --register")
        return 0

    # 6) update_product API 호출
    print(f"\n  update_product 호출 — originProductNo={NAVER_PRODUCT_NO}")
    from backend.purchase.services.naver_commerce_service import update_product
    partial = {
        "originProduct": {
            "originAreaInfo": {
                "originAreaCode": "03",
                "content": "상세페이지 참고",
                "importer": "Charis G",
            },
            "detailContent": new_html,
        }
    }
    res = update_product(NAVER_PRODUCT_NO, partial)
    if res:
        print(f"  ✓ update_product 성공")
        print(f"    response keys: {list(res.keys())[:10] if isinstance(res, dict) else res}")
    else:
        print(f"  ✗ update_product 실패")
        return 1
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stage", type=int, default=1, choices=[1, 2, 3, 4, 5])
    p.add_argument("--register", action="store_true",
                   help="Stage 3/4/5 실행 (없으면 dry_run)")
    args = p.parse_args()
    if args.stage == 1:
        return stage1()
    if args.stage == 2:
        return stage2()
    if args.stage == 3:
        return stage3(args.register)
    if args.stage == 4:
        return stage4(args.register)
    if args.stage == 5:
        return stage5_fix_naver(args.register)
    return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
