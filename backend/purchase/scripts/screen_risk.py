"""신규 enrichment draft 배치를 클린/리스크 필터로 사전 스크리닝 (읽기전용).
리스팅 전에 어떤 게 차단될지 + 필터 작동 확인용. title_en 기반 + manufacturer 기반."""
import os
import sqlite3
from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")


def main(since="2026-05-25"):
    from backend.purchase.services import clean_policy
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, asin, title_en, amazon_manufacturer FROM products "
        "WHERE business_model='purchase' AND status='draft' AND created_at >= ?", (since,)
    ).fetchall()
    ing_blocked = []
    mfr_blocked = []
    mfr_null = 0
    for r in rows:
        b, kw = clean_policy.check_prohibited_ingredients(r["title_en"] or "", "")
        if b:
            ing_blocked.append((r["asin"], (r["title_en"] or "")[:44], kw))
        if r["amazon_manufacturer"]:
            kb, kr = clean_policy.check_korean_manufacturer(r["amazon_manufacturer"])
            if kb:
                mfr_blocked.append((r["asin"], r["amazon_manufacturer"], kr))
        else:
            mfr_null += 1
    print(f"=== 사전 스크리닝 (draft {len(rows)}건, created>={since}) ===")
    print(f"금지성분(title_en) 차단: {len(ing_blocked)}")
    for s in ing_blocked[:20]:
        print(f"  · {s[0]} | {s[1]} | kw={s[2]}")
    print(f"한국제조사 차단: {len(mfr_blocked)} | amazon_manufacturer NULL(미분류): {mfr_null}")
    for s in mfr_blocked[:10]:
        print(f"  · {s[0]} | mfr={s[1]} | {s[2]}")
    print("\n※ KC(어린이제품)/카테고리 게이트는 카테고리 확정(리스팅 시점) 후 작동.")
    print("※ 한국제조사 게이트는 amazon_manufacturer 분류(mfr_classify_daemon/리스팅) 후 완전 작동.")


if __name__ == "__main__":
    main()
