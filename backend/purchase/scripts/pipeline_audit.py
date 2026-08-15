"""파이프라인 단계 감사 (funnel audit) — 임포트→리스팅 각 단계 진행/침묵실패 점검.

이번(2026-06) 사고들이 전부 "단계가 조용히 실패해도 모르고 지나간" 것이라, 단계별
통과 수 + 막힌/실패 지점(침묵실패)을 한 눈에 보여준다.

범위:
  --recent N   최근 N일 (products.created_at 기준, 기본 7)
  --batch STR  products.notes 또는 sourcing notes 에 STR 포함 (특정 임포트)
  --asins A,B  특정 ASIN
  --channel    coupang(기본) | smartstore

  python -m backend.purchase.scripts.pipeline_audit --recent 7
  python -m backend.purchase.scripts.pipeline_audit --asins B0CG8PP3M3,B08H1PVPFR
"""
import argparse, os, sqlite3
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("PA_DB_PATH",
                         str(os.path.join(os.path.dirname(__file__), "..", "purchase.db")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent", type=int, default=7)
    ap.add_argument("--batch", default="")
    ap.add_argument("--asins", default="")
    ap.add_argument("--channel", default="coupang")
    args = ap.parse_args()
    ch = args.channel

    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=180000")

    # 범위 WHERE
    where = "p.business_model='purchase'"
    params = []
    if args.asins:
        al = [a.strip() for a in args.asins.split(",") if a.strip()]
        where += f" AND p.asin IN ({','.join('?'*len(al))})"
        params += al
        scope_desc = f"ASIN {len(al)}개"
    elif args.batch:
        where += " AND (p.notes LIKE ? OR p.asin IN (SELECT asin FROM sourcing_candidates WHERE notes LIKE ?))"
        params += [f"%{args.batch}%", f"%{args.batch}%"]
        scope_desc = f"batch '{args.batch}'"
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.recent)).strftime("%Y-%m-%d")
        where += " AND p.created_at >= ?"
        params.append(cutoff)
        scope_desc = f"최근 {args.recent}일 (>= {cutoff})"

    def q(extra, p2=()):
        return con.execute(
            f"SELECT COUNT(*) FROM products p "
            f"LEFT JOIN listings_pa l ON l.product_id=p.id AND l.channel='{ch}' "
            f"WHERE {where} {extra}", params + list(p2)).fetchone()[0]

    total = q("")
    # 단계별 통과
    ai_ok = q("AND p.ai_processed_at IS NOT NULL AND p.title_ko IS NOT NULL AND p.title_ko != '' "
              "AND p.title_ko NOT LIKE '[브랜드%'")
    cost_ok = q("AND p.cost_usd > 0")
    has_listing = q("AND l.id IS NOT NULL")
    excluded = q("AND l.status='excluded'")              # 정책차단(정상) — 실패 아님
    active = q("AND l.id IS NOT NULL AND l.status != 'excluded'")  # 차단 외 listing
    has_cpid = q("AND l.channel_product_id IS NOT NULL AND l.channel_product_id != ''")
    approved = q("AND l.coupang_status_name = '승인완료'") if ch == "coupang" else q("AND l.status='listed'")
    rejected = q("AND l.coupang_status_name = '승인반려'") if ch == "coupang" else 0
    pending_rev = q("AND l.coupang_status_name IN ('승인대기중','심사중')") if ch == "coupang" else 0

    unsynced = has_cpid - approved - rejected - pending_rev
    print(f"=== 파이프라인 단계 감사 [{ch}] — {scope_desc} ===")
    print(f"\n  단계별 통과 (funnel): [cpid 기록 = 쿠팡 등록 성공 지표]")
    rows = [
        ("① products(promoted)", total),
        ("② AI 번역(title_ko)", ai_ok),
        ("③ 원가(cost_usd>0)", cost_ok),
        ("④ 채널발송(차단외 listing)", active),
        ("⑤ 업로드(cpid 기록=쿠팡등록)", has_cpid),
    ]
    prev = total
    for name, c in rows:
        drop = prev - c if c <= prev else 0
        bar = "█" * int(c / max(total, 1) * 20)
        print(f"    {name:28s} {c:6d}  {bar:<20s} {'(-'+str(drop)+')' if drop else ''}")
        prev = c
    print(f"    └ 정책차단(excluded,정상) {excluded}")
    print(f"\n  쿠팡 승인상태 (등록 {has_cpid} 중, ※coupang_status_name 동기화 의존):")
    print(f"    승인완료 {approved} / 심사중 {pending_rev} / 승인반려 {rejected} / 미동기화 {unsynced if unsynced>0 else 0}")

    # ── 침묵 실패 / 막힘 플래그 ──
    print(f"\n  ⚠️ 이상 탐지:")
    flags = []
    cand = con.execute("SELECT COUNT(*) FROM sourcing_candidates").fetchone()[0]
    if cand > 0:
        flags.append(f"sourcing_candidates {cand}건 잔존 — promote 미완/크래시 가능 (정상이면 0)")
    no_ai = total - ai_ok
    if no_ai > 0:
        flags.append(f"AI 미완 {no_ai}건 (title_ko 없음/placeholder) — AI 침묵실패 의심")
    no_cost = total - cost_ok
    if no_cost > 0:
        flags.append(f"원가 없음 {no_cost}건 (cost_usd≤0) — sale_krw 산정불가/exclude 위험")
    stuck = q("AND p.status='draft' AND l.id IS NULL")
    if stuck > 0:
        flags.append(f"미발송 draft {stuck}건 — 채널발송/리스팅 안 됨 (트리거 없음 의심)")
    # ★ 진짜 orphan = status='listed' 인데 cpid 없음 (excluded/pending 제외)
    orphan = q("AND l.status='listed' AND (l.channel_product_id IS NULL OR l.channel_product_id='')")
    if orphan > 0:
        flags.append(f"★ orphan {orphan}건 — status=listed 인데 cpid 미기록(쿠팡 등록됐는데 DB 추적안됨)")
    if rejected > 0:
        flags.append(f"승인반려 {rejected}건 — 사유 확인 필요 (히스토리 API)")
    if not flags:
        print("    ✅ 이상 없음 — 모든 단계 정상 진행")
    else:
        for f in flags:
            print(f"    • {f}")

    # 막힌 샘플 (진짜 막힘만 — excluded 제외)
    if stuck > 0 or orphan > 0 or no_ai > 0:
        print(f"\n  막힌 샘플 (asin / 상태):")
        sample = con.execute(
            f"SELECT p.asin, p.status pstat, "
            f"CASE WHEN p.title_ko IS NULL OR p.title_ko='' THEN 'AI없음' ELSE 'AI ok' END ai, "
            f"COALESCE(l.status,'(행없음)') lst, "
            f"CASE WHEN l.channel_product_id IS NULL THEN 'cpid없음' ELSE 'cpid ok' END cp "
            f"FROM products p LEFT JOIN listings_pa l ON l.product_id=p.id AND l.channel='{ch}' "
            f"WHERE {where} AND (p.ai_processed_at IS NULL OR (p.status='draft' AND l.id IS NULL) "
            f"OR (l.status='listed' AND l.channel_product_id IS NULL)) LIMIT 10", params).fetchall()
        for s in sample:
            print(f"    {s['asin']}  prod={s['pstat']}  {s['ai']}  listing={s['lst']}  {s['cp']}")
    con.close()


if __name__ == "__main__":
    main()
