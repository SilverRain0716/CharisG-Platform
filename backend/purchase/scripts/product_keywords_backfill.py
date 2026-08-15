"""product_keywords 백필 — 5/1 부터 누락된 22,952+ product 에 키워드 INSERT.

source 우선순위:
  1. products.seo_tags (JSON array) 의 top 5
  2. products.title_ko 의 첫 단어
  3. products.title_en 의 첫 단어

첫 번째 키워드만 is_primary=1, 나머지 4개는 secondary.

이 스크립트 1회 실행 후 fix #0 (ai_processor 패치) 가 신규 product 의 키워드 자동 INSERT 담당.
"""
import sys
import json
sys.path.insert(0, "/home/ubuntu/CharisG-Platform/charisg-platform")

from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")

import sqlite3
from backend.purchase.services.category_mapper import _normalize_keyword

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"


def _extract_keywords(row) -> list[tuple[str, int]]:
    """반환: [(keyword, is_primary), ...] (최대 5개, 첫 항목만 primary=1)."""
    candidates: list[str] = []
    # 1) seo_tags
    seo_tags_raw = row["seo_tags"] or ""
    if seo_tags_raw and seo_tags_raw != "[]":
        try:
            tags = json.loads(seo_tags_raw)
            if isinstance(tags, list):
                candidates.extend(str(t) for t in tags if t)
        except Exception:
            pass
    # 2) title_ko fallback (seo_tags 비어있을 때)
    if not candidates and row["title_ko"]:
        tokens = str(row["title_ko"]).split()
        if tokens:
            candidates.append(tokens[0])
    # 3) title_en fallback
    if not candidates and row["title_en"]:
        tokens = str(row["title_en"]).split()
        if tokens:
            candidates.append(tokens[0])

    # 정규화 + dedup
    seen = set()
    result: list[tuple[str, int]] = []
    for idx, c in enumerate(candidates[:5]):
        norm = _normalize_keyword(c)
        if norm and norm not in seen:
            seen.add(norm)
            result.append((norm, 1 if not result else 0))
    return result


def main():
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT id, seo_tags, title_ko, title_en
             FROM products
            WHERE id NOT IN (SELECT product_id FROM product_keywords)"""
    ).fetchall()

    print(f"=== product_keywords backfill: {len(rows)} targets ===")

    inserted_rows = 0
    inserted_pids = 0
    no_keyword_pids = 0

    for i, r in enumerate(rows, 1):
        kws = _extract_keywords(r)
        if not kws:
            no_keyword_pids += 1
            continue
        for kw, is_pri in kws:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO product_keywords "
                    "(product_id, keyword, is_primary) VALUES (?, ?, ?)",
                    (r["id"], kw, is_pri),
                )
                inserted_rows += 1
            except Exception as e:
                print(f"  FAIL pid={r['id']} kw={kw}: {e}")
        inserted_pids += 1
        if i % 1000 == 0:
            conn.commit()
            print(
                f"  progress {i}/{len(rows)}  pids_with_kw={inserted_pids} "
                f"no_kw={no_keyword_pids} kw_rows={inserted_rows}"
            )

    conn.commit()
    print(
        f"\nDONE: pids_with_kw={inserted_pids}, no_kw={no_keyword_pids}, "
        f"total_kw_rows={inserted_rows}"
    )

    # 최종 검증
    after_total = conn.execute("SELECT COUNT(DISTINCT product_id) FROM product_keywords").fetchone()[0]
    print(f"  product_keywords distinct product_id: {after_total}")
    conn.close()


if __name__ == "__main__":
    main()
