"""시트 큐 등록 — sheet_queue 에 status='queued' 행 추가 → 워커가 자동 처리.
사용: .venv/bin/python -m backend.purchase.scripts.enqueue_sheet "<url>" "<label>" "<channels>"
  channels: 'coupang' 또는 'smartstore,coupang' (기본 coupang)"""
import sys
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)


def main(url, label, channels):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as c:
        cur = c.execute(
            "INSERT INTO sheet_queue (sheet_url, sheet_label, status, target_channels, queued_at) "
            "VALUES (?, ?, 'queued', ?, ?)", (url, label or None, channels, now))
        sid = cur.lastrowid
    print(f"✓ 큐 등록 완료 — sid={sid} label='{label}' channels='{channels}' status=queued")
    print("  워커가 30초 내 자동 처리 시작 (import→AI→카테고리→리스팅)")


if __name__ == "__main__":
    url = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else ""
    channels = sys.argv[3] if len(sys.argv) > 3 else "coupang"
    main(url, label, channels)
