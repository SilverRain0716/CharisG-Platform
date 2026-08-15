"""쿠팡 listed 상품 중 번역 안 된(영문 잔존) 상품 파악 — 읽기 전용 분석.

판정: title_ko 의 한글 글자수로. 한글<2 = 사실상 미번역(영문 잔존).
      title_ko 비어있으면 listing 시 title_en(영문) fallback → 미번역 노출.
"""
import os
import sqlite3
from collections import Counter

DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "backend/purchase/purchase.db",
)


def hangul(s: str) -> int:
    return sum(1 for ch in (s or "") if "가" <= ch <= "힣")


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT p.id, p.title_en, p.title_ko, p.seo_title,
                  substr(p.created_at,1,10) d, p.ai_processed_at
           FROM products p
           JOIN listings_pa l ON l.product_id = p.id
           WHERE l.channel='coupang' AND l.status='listed'"""
    ).fetchall()

    total = len(rows)
    empty_ko = untrans = ok = 0
    by_date = Counter()
    samples = []
    for r in rows:
        tk = r["title_ko"]
        if not tk or not tk.strip():
            empty_ko += 1
            cat = "EMPTY_KO"
        elif hangul(tk) < 2:
            untrans += 1
            cat = "ENGLISH"
        else:
            ok += 1
            continue
        by_date[r["d"]] += 1
        if len(samples) < 20:
            samples.append((cat, r["id"], (tk or "(빈값)")[:45], (r["title_en"] or "")[:40]))

    print(f"쿠팡 listed 총: {total}")
    print(f"  정상 번역(한글 있음): {ok}")
    print(f"  ★영문 잔존(한글<2): {untrans}")
    print(f"  ★title_ko 빈값(영문 fallback): {empty_ko}")
    print(f"  미번역 합계: {untrans + empty_ko} ({100*(untrans+empty_ko)/total:.1f}%)")
    print()
    print("미번역 생성일자별 분포 (TOP):")
    for d, n in by_date.most_common(12):
        print(f"  {d}: {n}")
    print()
    print("샘플 (cat | pid | title_ko | title_en):")
    for cat, pid, tk, te in samples:
        print(f"  [{cat}] {pid} | {tk} | {te}")


if __name__ == "__main__":
    main()
