"""
coupon_publish_watcher.py — sheet_queue id=5 done 감지 → 쿠폰 발급 자동 실행.

- 5분 간격 polling
- sheet_queue.status='done' AND finished_at != NULL 이면 발급 트리거
- 한 번 트리거되면 self-exit (재실행 방지 파일 마커)
- error/cancelled 면 종료

사용:
  nohup python3 -m backend.purchase.scripts.coupon_publish_watcher \
    --queue-id 5 > /tmp/coupon_watcher.log 2>&1 &
"""
import argparse
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

POLL_INTERVAL = 300  # 5분
MARKER_PATH = Path("/tmp/coupon_publish_triggered.marker")


def get_queue_status(db_path: Path, qid: int) -> dict | None:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id, status, current_step, finished_at, error_message FROM sheet_queue WHERE id=?",
        (qid,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue-id", type=int, required=True, help="trigger when this sheet_queue.id done")
    ap.add_argument("--also-wait-for", type=int, action="append", default=[],
                    help="이 큐들도 done 이어야 함 (반복 가능)")
    ap.add_argument("--start-at", type=str, help="쿠폰 startAt 명시")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    if MARKER_PATH.exists():
        logger.warning(f"이미 트리거됨 ({MARKER_PATH}). 재시작하려면 마커 파일 삭제 후 재실행.")
        sys.exit(0)

    repo = Path(__file__).resolve().parents[3]
    db = repo / "backend" / "purchase" / "purchase.db"
    targets = [args.queue_id] + list(args.also_wait_for)
    logger.info(f"watching sheet_queue ids={targets}, poll={POLL_INTERVAL}s, db={db}")

    while True:
        all_done = True
        any_error = False
        for qid in targets:
            r = get_queue_status(db, qid)
            if not r:
                logger.error(f"  qid={qid} not found in sheet_queue")
                sys.exit(1)
            if r["status"] in ("error", "cancelled"):
                logger.error(f"  qid={qid} status={r['status']} — abort. err={r.get('error_message')}")
                any_error = True
                break
            if r["status"] != "done":
                all_done = False
                logger.info(f"  qid={qid} status={r['status']} step={r['current_step']}")
        if any_error:
            sys.exit(2)
        if all_done:
            logger.info(f"=== 모든 큐 done. 쿠폰 발급 시작 ===")
            MARKER_PATH.write_text(f"triggered at {time.time()}\n")
            cmd = [
                sys.executable, "-m", "backend.purchase.scripts.coupang_publish_coupon_policy",
                "--execute",
                "--days", str(args.days),
            ]
            if args.start_at:
                cmd += ["--start-at", args.start_at]
            logger.info(f"  cmd: {' '.join(cmd)}")
            try:
                subprocess.run(cmd, cwd=str(repo), check=True)
            except subprocess.CalledProcessError as e:
                logger.error(f"  publish 실패 (exit {e.returncode})")
                sys.exit(3)
            logger.info("=== watcher 종료 ===")
            sys.exit(0)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
