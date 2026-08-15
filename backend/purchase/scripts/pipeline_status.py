"""pipeline_status — CLI 도구.

사용:
  python -m backend.purchase.scripts.pipeline_status            # 전체 종합
  python -m backend.purchase.scripts.pipeline_status --sid 10   # 특정 시트
  python -m backend.purchase.scripts.pipeline_status --stuck    # stuck items
  python -m backend.purchase.scripts.pipeline_status --db       # DB lock 측정
"""
import argparse

from backend.purchase.database import get_db

STAGE_ORDER = [
    "imported", "promote_done", "ai_done", "channel_send_done",
    "upload_done", "kr_verify_done", "forwarder_done", "coupon_done",
    "done", "failed", "excluded",
]


def cmd_overview() -> None:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT stage, COUNT(*) cnt FROM pipeline_items GROUP BY stage"
        ).fetchall()
    by_stage = {s: 0 for s in STAGE_ORDER}
    for r in rows:
        by_stage[r["stage"]] = r["cnt"]
    total = sum(by_stage.values())

    print(f"=== pipeline_items 전체 {total}건 ===")
    for s in STAGE_ORDER:
        bar = "#" * (by_stage[s] * 40 // max(total, 1))
        print(f"  {s:22s} {by_stage[s]:>8d}  {bar}")

    print("\n=== stuck (30분+) ===")
    with get_db() as conn:
        stuck = conn.execute(
            "SELECT stage, COUNT(*) cnt FROM vw_pipeline_stuck GROUP BY stage"
        ).fetchall()
    if not stuck:
        print("  (없음)")
    else:
        for r in stuck:
            print(f"  {r['stage']:22s} {r['cnt']:>4d}건")


def cmd_sheet(sid: int) -> None:
    with get_db() as conn:
        meta = conn.execute(
            "SELECT id, status, current_step, imported, sheet_label "
            "FROM sheet_queue WHERE id=?", (sid,),
        ).fetchone()
        if not meta:
            print(f"sheet_queue id={sid} 없음")
            return
        print(f"=== sheet {sid}: {meta['sheet_label']} ===")
        print(f"  status={meta['status']} step='{meta['current_step']}' imported={meta['imported']}")
        rows = conn.execute(
            "SELECT stage, COUNT(*) cnt FROM pipeline_items "
            "WHERE sheet_queue_id=? GROUP BY stage", (sid,),
        ).fetchall()
    print("\n  stage 별 카운트:")
    for r in rows:
        print(f"    {r['stage']:22s} {r['cnt']:>6d}")


def cmd_stuck(limit: int = 20) -> None:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, asin, stage, retry_count, stuck_min, error_message "
            "FROM vw_pipeline_stuck ORDER BY stuck_min DESC LIMIT ?", (limit,),
        ).fetchall()
    if not rows:
        print("stuck items 없음")
        return
    print(f"=== stuck items (top {limit}) ===")
    for r in rows:
        msg = (r["error_message"] or "")[:60]
        print(
            f"  id={r['id']:>6d} asin={r['asin']} stage={r['stage']:20s} "
            f"retry={r['retry_count']} stuck={r['stuck_min']}min "
            f"err='{msg}'"
        )


def cmd_db() -> None:
    print("=== DB lock health (최근 1h) ===")
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM vw_db_lock_health").fetchall()
    if not rows:
        print("  (데이터 없음 — metrics 미수집)")
        return
    print(f"  {'stage':22s} {'avg_wait':>10s} {'max_wait':>10s} "
          f"{'avg_tx':>10s} {'max_tx':>10s} {'chunks':>8s}")
    for r in rows:
        print(
            f"  {r['stage']:22s} "
            f"{r['avg_wait_ms'] or 0:>10.1f} "
            f"{r['max_wait_ms'] or 0:>10d} "
            f"{r['avg_tx_ms'] or 0:>10.1f} "
            f"{r['max_tx_ms'] or 0:>10d} "
            f"{r['chunks_1h']:>8d}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", type=int, help="특정 시트 진행")
    ap.add_argument("--stuck", action="store_true", help="stuck items 리스트")
    ap.add_argument("--db", action="store_true", help="DB lock 측정")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    if args.sid:
        cmd_sheet(args.sid)
    elif args.stuck:
        cmd_stuck(args.limit)
    elif args.db:
        cmd_db()
    else:
        cmd_overview()


if __name__ == "__main__":
    main()
