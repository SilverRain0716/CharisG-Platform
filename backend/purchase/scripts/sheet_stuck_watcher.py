"""sheet_queue stuck watcher — 1시간마다 진행 정체 sid 자동 재개.

stuck 판정: 1시간 동안 imported/promoted/detailed/coupang_listed/smartstore_listed
모두 동일 (변동 0) + status NOT IN ('queued','done','error','cancelled').

조치:
  - 관련 batch_jobs.status='running' → 'cancelled'
  - sheet_queue.status='queued' (worker 30초 폴링으로 자동 재pick)

systemd service 로 등록 (Restart=always). 메모리 정체 비교는 단일 process 내부.
"""
import logging
import sqlite3
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sheet-stuck-watcher")

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
# 2026-05-12 fix: 1h → 4h. promote 잡 (~3h) 끝나기 전 false-positive reset 방지.
POLL_INTERVAL_SEC = 14400  # 4시간
TERMINAL = ("queued", "done", "error", "cancelled")
LONG_JOB_TYPES = (
    "sourcing_promote",
    "ai_detail",
    "coupang_upload",
    "smartstore_upload",
    "kr_shipping_verify_batch",
    "kr_shipping_verify",
)


def _snapshot() -> dict:
    """현재 in-progress sheet_queue 스냅샷 + sid 별 batch_jobs 진행 합산.

    sheet_queue 자체 카운터는 단계 끝에만 update 되어 진행 중 변동 0 → false
    stuck. 이를 방지하기 위해 batch_jobs running 의 processed 합도 snapshot 에
    포함. 단 sid 별 격리: 각 sid 의 started_at 이후 시작된 running 잡만 진행값으로
    인정 → orphan (다른 sid 작업 중) 도 stuck 으로 정상 판정. (2026-05-15 fix)
    """
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, status, started_at, imported, promoted, detailed, "
        "coupang_listed, smartstore_listed, smartstore_failed, coupang_failed "
        "FROM sheet_queue WHERE status NOT IN ('queued','done','error','cancelled')"
    ).fetchall()
    out: dict = {}
    ph = ",".join("?" * len(LONG_JOB_TYPES))
    for r in rows:
        bj = conn.execute(
            f"SELECT COALESCE(SUM(processed), 0) FROM batch_jobs "
            f"WHERE status='running' AND job_type IN ({ph}) "
            f"AND started_at >= ?",
            (*LONG_JOB_TYPES, r["started_at"] or "1970-01-01"),
        ).fetchone()[0]
        out[r["id"]] = dict(r, _bj_progress=bj)
    conn.close()
    return out


def _detect_orphans() -> list[int]:
    """직렬화 worker 가정에서, in-progress 중 가장 최근 started_at 1건만 실제 작업 sheet.
    나머지는 PA-API 재시작 등으로 abandon 된 orphan — 즉시 reset (4h snapshot 비교 불요).
    """
    conn = sqlite3.connect(DB, timeout=30)
    rows = conn.execute(
        "SELECT id FROM sheet_queue "
        "WHERE status NOT IN ('queued','done','error','cancelled') "
        "ORDER BY COALESCE(started_at, queued_at) DESC"
    ).fetchall()
    conn.close()
    if len(rows) <= 1:
        return []
    return [r[0] for r in rows[1:]]


_POST_UPLOAD_STATUSES = frozenset({
    "uploading_smartstore", "uploading_coupang",
    "coupon_applying", "cleaning",
})
# 2026-05-15: kr_verifying, forwarder_repricing 가 pre-upload 단계로 이동됨에 따라
# orphan 회수 시 done 으로 닫을 수 없음 (업로드 안 됐을 수 있음) → error 분기.


def _reset_stuck(sid: int) -> None:
    """stuck sid 의 batch_jobs cancel + sheet_queue.status='queued' reset."""
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA busy_timeout=180000")
    ph = ",".join("?" * len(LONG_JOB_TYPES))
    bj_cur = conn.execute(
        f"UPDATE batch_jobs SET status='cancelled', finished_at=datetime('now'), "
        f"error_message='sheet-stuck-watcher: 4h 정체 cancel' "
        f"WHERE status='running' AND job_type IN ({ph})",
        LONG_JOB_TYPES,
    )
    bj_cancelled = bj_cur.rowcount
    conn.execute(
        "UPDATE sheet_queue SET status='queued', "
        "current_step='stuck-watcher 자동 재개' WHERE id=?",
        (sid,),
    )
    conn.commit()
    conn.close()
    logger.warning(
        f"[stuck-watcher] sid={sid} reset 완료 (batch cancelled: {bj_cancelled})"
    )


def _close_orphan(sid: int) -> None:
    """Orphan sid 종결 — batch_jobs 는 다른 sid 의 active 잡일 수 있어 보존.

    sheet_queue_worker._recover_orphans() 와 동일 정책:
      - 업로드 이후 단계 + 카운터 살아있음 → done
      - 그 외 → error (수동 재처리)
    """
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=180000")
    r = conn.execute(
        "SELECT status, smartstore_listed, smartstore_failed, coupang_listed, coupang_failed "
        "FROM sheet_queue WHERE id=?",
        (sid,),
    ).fetchone()
    if r is None:
        conn.close()
        return
    status = r["status"]
    ss_total = (r["smartstore_listed"] or 0) + (r["smartstore_failed"] or 0)
    cu_total = (r["coupang_listed"] or 0) + (r["coupang_failed"] or 0)
    if status in _POST_UPLOAD_STATUSES and (ss_total or cu_total):
        conn.execute(
            "UPDATE sheet_queue SET status='done', "
            "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
            "current_step=? WHERE id=?",
            (
                f"stuck-watcher orphan close — {status} abandon, 업로드 결과 보존 후 done",
                sid,
            ),
        )
        decision = "done"
    else:
        conn.execute(
            "UPDATE sheet_queue SET status='error', "
            "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
            "error_message=?, current_step=? WHERE id=?",
            (
                f"stuck-watcher orphan: worker abandoned at {status} — 수동 재처리 필요",
                f"stuck-watcher orphan close — {status} 단계 abandon",
                sid,
            ),
        )
        decision = "error"
    conn.commit()
    conn.close()
    logger.warning(
        f"[stuck-watcher] orphan sid={sid} {status}→{decision} "
        f"(SS {r['smartstore_listed']}/{r['smartstore_failed']} "
        f"CU {r['coupang_listed']}/{r['coupang_failed']}, batch_jobs 보존)"
    )


def _check_orphan_sourcing() -> None:
    """sourcing_candidates 잔여 + in-progress sheet_queue 0 → 최신 done sid 재pick.

    sid 가 PA API restart 등으로 잘못 'done' 처리됐는데 sourcing_candidates 에는
    promote 안 된 row 가 남아있는 케이스 (2026-05-12 sid=10 사고 패턴).
    """
    conn = sqlite3.connect(DB, timeout=30)
    src_cnt = conn.execute("SELECT COUNT(*) FROM sourcing_candidates").fetchone()[0]
    in_progress = conn.execute(
        "SELECT COUNT(*) FROM sheet_queue "
        "WHERE status NOT IN ('queued','done','error','cancelled')"
    ).fetchone()[0]
    if src_cnt == 0 or in_progress > 0:
        conn.close()
        return
    # 최신 done sid 찾아 reset
    row = conn.execute(
        "SELECT id FROM sheet_queue WHERE status='done' "
        "ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        conn.close()
        return
    sid = row[0]
    conn.execute("PRAGMA busy_timeout=180000")
    conn.execute(
        "UPDATE sheet_queue SET status='queued', "
        "current_step='stuck-watcher: sourcing_candidates 잔여 자동 재pick', "
        "finished_at=NULL WHERE id=?",
        (sid,),
    )
    conn.commit()
    conn.close()
    logger.warning(
        f"[stuck-watcher] orphan sourcing_candidates {src_cnt}건 + in_progress=0 "
        f"→ sid={sid} done→queued reset (자동 재pick)"
    )


def main() -> None:
    logger.info(f"[stuck-watcher] 기동 (poll={POLL_INTERVAL_SEC}s)")
    prev = _snapshot()
    logger.info(
        f"[stuck-watcher] initial in-progress: {len(prev)} sids: {list(prev.keys())}"
    )
    while True:
        time.sleep(POLL_INTERVAL_SEC)
        try:
            # 0) Orphan 즉시 종결 — 직렬화 worker 가정에서 in-progress 2건 이상이면
            # 가장 최근 started_at 외 모두 abandon 된 orphan. 4h 대기 불요. (2026-05-15)
            try:
                orphans = _detect_orphans()
                for sid in orphans:
                    try:
                        _close_orphan(sid)
                    except Exception:
                        logger.exception(f"[stuck-watcher] orphan sid={sid} close 예외")
            except Exception:
                logger.exception("[stuck-watcher] orphan 검출 예외")

            # 1) in-progress 정체 검사
            cur = _snapshot()
            stuck_sids = []
            for sid, snap in cur.items():
                ps = prev.get(sid)
                if ps is not None and snap == ps:
                    stuck_sids.append(sid)
                    logger.warning(
                        f"[stuck-watcher] sid={sid} 정체 감지: status={snap['status']} "
                        f"imp={snap['imported']} prom={snap['promoted']} "
                        f"det={snap['detailed']} cu_listed={snap['coupang_listed']}"
                    )
            for sid in stuck_sids:
                try:
                    _reset_stuck(sid)
                except Exception:
                    logger.exception(f"[stuck-watcher] sid={sid} reset 예외")

            # 2) orphan sourcing_candidates 검사 (2026-05-12 추가)
            try:
                _check_orphan_sourcing()
            except Exception:
                logger.exception("[stuck-watcher] orphan 검사 예외")

            if not stuck_sids:
                logger.info(
                    f"[stuck-watcher] cycle ok — {len(cur)} in-progress 진행 중"
                )
            prev = cur
        except Exception:
            logger.exception("[stuck-watcher] cycle 예외")


if __name__ == "__main__":
    main()
