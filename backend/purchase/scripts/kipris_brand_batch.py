# -*- coding: utf-8 -*-
"""KIPRIS 브랜드 배치 게이트 (2026-07-05) — Stage 2 예방 자동화.

리스팅 대상(노출순) 브랜드를 KIPRIS 권리자 대조(strict) → kipris_brand_cache 적재.
strict-flagged(국내 권리자 확정) 브랜드는 브랜드 블랙리스트에 자동 추가(--commit).
→ 기존 블랙리스트 게이트가 전 경로(리스터+마이그) 차단. 라이브 핫패스 무변경.

Gemini 소진 대응: 이번 실행은 '한글 브랜드' + '이미 음역캐시(brand_ko_cache.korean!="")된
영문 브랜드'만 스크리닝(신규 영문 음역은 --gemini-translit 로 별도, Gemini 회복 후).
KIPRIS 월 1,000 쿼터 보호: --kipris-budget cap + '대기'(쿼터초과) 감지 시 즉시 중단.

실행:
  PYTHONPATH=<repo> .venv/bin/python -m backend.purchase.scripts.kipris_brand_batch \
      --kipris-budget 200            # dry-run (블랙리스트 미변경)
  ... --kipris-budget 200 --commit   # flagged → 블랙리스트 실제 추가
  ... --gemini-translit 100          # (Gemini 회복 후) 영문 브랜드 음역 선충전
"""
import argparse
import json
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from backend.purchase.database import get_db
from backend.purchase.routers import ip_screening as ips
from backend.purchase.services.coupang_lister import _load_brand_blocklist, _is_brand_blocked

# 리스팅 노출 대상 status (실제 등록/등록예정)
_BOUND_STATUS = ("listed", "paused", "pending", "promoting")


def _has_hangul(s: str) -> bool:
    return any("가" <= ch <= "힣" for ch in (s or ""))


def _candidate_brands(conn, limit_scan: int = 6000):
    """리스팅 대상 · 미판정 브랜드를 노출(상품수) 내림차순으로."""
    ph = ",".join("?" * len(_BOUND_STATUS))
    rows = conn.execute(
        f"""SELECT p.brand AS brand, COUNT(*) AS n
            FROM products p JOIN listings_pa l
              ON l.product_id=p.id AND l.channel='coupang'
            WHERE p.brand IS NOT NULL AND trim(p.brand)!=''
              AND l.status IN ({ph})
              AND p.brand NOT IN (SELECT brand FROM kipris_brand_cache)
            GROUP BY p.brand ORDER BY n DESC LIMIT ?""",
        (*_BOUND_STATUS, limit_scan),
    ).fetchall()
    return [(r["brand"], r["n"]) for r in rows]


def _has_translit(conn, brand: str) -> bool:
    r = conn.execute("SELECT korean FROM brand_ko_cache WHERE brand=?", (brand,)).fetchone()
    return bool(r and (r["korean"] or "").strip())


def _query_for(conn, brand: str) -> str:
    """블랙리스트 sync용 조회어 재산출(한글=자신, 영문=음역캐시)."""
    if _has_hangul(brand):
        return brand.strip()
    r = conn.execute("SELECT korean FROM brand_ko_cache WHERE brand=?", (brand,)).fetchone()
    return (r["korean"].strip() if r and r["korean"] else "")


def _tight_match(query: str, holder: str) -> bool:
    """토큰/접두 매칭 — 음역(query)이 권리자명(holder)의 '단어' 또는 '토큰 접두'일 때만 인정.
    부분문자열 오탐 제거: 파워넷⊄에이치엠파워넷, 질리스⊄아질리스, 스카치⊄합스카치는 탈락;
    뵈르너∈[…뵈르너…], 에버라스트=prefix, 슈피겐→슈피겐뷰티(토큰접두)는 인정."""
    q = (query or "").strip()
    if len(q) < 3 or not holder:
        return False
    toks = holder.replace("-", " ").replace(",", " ").replace(".", " ").split()
    return any(t == q or t.startswith(q) for t in toks)


def gemini_translit(conn, brands, budget):
    """영문 브랜드 한글 음역 선충전(Gemini). 소진 시 조용히 스킵."""
    ips._ensure_ko_cache(conn)
    done = 0
    for b, _n in brands:
        if done >= budget:
            break
        if _has_hangul(b) or _has_translit(conn, b):
            continue
        ko = ips._brand_korean(conn, b)  # Gemini + 캐시(실패 시 '')
        done += 1
        if (ko or "").strip():
            print(f"  음역 {b} -> {ko}")
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kipris-budget", type=int, default=150, help="KIPRIS 라이브 조회 상한(월 1000 보호)")
    ap.add_argument("--gemini-translit", type=int, default=0, help="영문 브랜드 음역 선충전 건수(Gemini)")
    ap.add_argument("--approve", default="", help="블랙리스트에 추가할 승인 브랜드(콤마구분). 후보와 교집합만 추가")
    ap.add_argument("--scan", type=int, default=6000, help="후보 스캔 상한")
    args = ap.parse_args()

    with get_db() as conn:
        ips._ensure_kipris_cache(conn)
        ips._ensure_ko_cache(conn)
        cands = _candidate_brands(conn, args.scan)
        print(f"[후보] 리스팅대상·미판정 브랜드 {len(cands)}개(노출순, 상위 {args.scan} 스캔)")

        # (옵션) Gemini 음역 선충전
        if args.gemini_translit > 0:
            n = gemini_translit(conn, cands, args.gemini_translit)
            print(f"[음역] Gemini 선충전 시도 {n}건")

        # 이번 실행 스크리닝 대상: 한글 or 음역보유 (Gemini 불필요)
        screenable = [(b, n) for (b, n) in cands if _has_hangul(b) or _has_translit(conn, b)]
        print(f"[스크리닝 가능] 한글/음역보유 {len(screenable)}개 (KIPRIS budget={args.kipris_budget})")

        flagged = []
        used = passed = 0
        for b, n in screenable:
            if used >= args.kipris_budget:
                print(f"[중단] KIPRIS budget {args.kipris_budget} 소진")
                break
            res = ips._screen_brand_multiform(conn, b)
            if res.get("status") == "대기":  # 쿼터초과/오류 → 캐시 미저장, 중단
                print(f"[중단] KIPRIS '대기'({res.get('reason','')[:40]}) — 쿼터 리셋 후 재실행")
                break
            used += 1
            ips._kipris_cache_put(conn, b, res)
            if res.get("flagged"):
                top = (res.get("matches") or [{}])[0].get("name", "")
                flagged.append((b, n, res.get("query", ""), top))
            else:
                passed += 1
        conn.commit()

        print(f"\n=== 스크리닝 결과: 조회 {used} / 통과 {passed} / (raw)flagged {len(flagged)} ===")

        # ── 블랙리스트 sync: kipris_brand_cache 의 flagged 전수 + 토큰/접두 정밀필터 ──
        bl = _load_brand_blocklist()
        rows = conn.execute(
            "SELECT brand, top_name FROM kipris_brand_cache WHERE flagged=1"
        ).fetchall()
        new_flags = []
        rejected = 0
        print(f"[sync] 캐시 flagged {len(rows)}건 → 토큰/접두 정밀필터")
        for r in rows:
            b = r["brand"]; top = r["top_name"] or ""
            q = _query_for(conn, b)
            if not _tight_match(q, top):
                rejected += 1
                continue  # 부분문자열 오탐 제거
            already = bool(_is_brand_blocked(b, "", bl)) or b in bl
            tag = "[이미차단]" if already else "★신규"
            print(f"  [확정] {b:22s} (→{q}, 권리자:{top[:24]}) {tag}")
            if not already:
                new_flags.append(b)
        print(f"[sync] 정밀필터 통과 {len(rows)-rejected} / 오탐제거 {rejected}")

        if not new_flags:
            print("\n신규 차단 후보 없음.")
            return
        print(f"\n신규 차단 후보 {len(new_flags)}개 (★사람 승인 필요 — 오탐 가능):")
        print("  " + ", ".join(new_flags))

        approve = {s.strip() for s in args.approve.split(",") if s.strip()}
        if not approve:
            print("\n(검토 모드 — 블랙리스트 미변경. 승인분만 추가하려면 --approve \"BrandA,BrandB\")")
            return

        to_add = [b for b in new_flags if b in approve]
        skipped_unknown = approve - set(new_flags)
        if skipped_unknown:
            print(f"[주의] 후보에 없어 무시: {', '.join(skipped_unknown)}")
        if not to_add:
            print("승인 목록이 후보와 교집합 없음 — 추가 안 함.")
            return

        row = conn.execute("SELECT value FROM settings WHERE key='coupang.brand_blocklist'").fetchone()
        cur = json.loads(row["value"]) if row and row["value"] else []
        Path("/tmp/brand_blocklist.preKipris.json").write_text(
            json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        existing = {str(x).strip().lower() for x in cur}
        added = []
        for b in to_add:
            if b.strip().lower() not in existing:
                cur.append(b); existing.add(b.strip().lower()); added.append(b)
        conn.execute("UPDATE settings SET value=? WHERE key='coupang.brand_blocklist'",
                     (json.dumps(cur, ensure_ascii=False),))
        conn.commit()
        print(f"[승인추가] {len(added)}개 → 총 {len(cur)}: {', '.join(added)} (백업 /tmp/brand_blocklist.preKipris.json)")


if __name__ == "__main__":
    main()
