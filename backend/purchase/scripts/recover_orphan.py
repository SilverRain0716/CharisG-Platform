"""삭제됐으나 재등록 실패한 고아 상품 복구 — 자동매칭(category=0)으로 재등록해 온라인 복구.
사용: .venv/bin/python -m backend.purchase.scripts.recover_orphan 47317 [47299 ...]"""
import sys
import os
from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)


def main(pids):
    from backend.purchase.services import coupang_lister
    for pid in pids:
        pid = int(pid)
        with get_db() as c:
            c.execute(
                "UPDATE listings_pa SET channel_product_id=NULL, coupang_category_code=NULL, "
                "coupang_auto_matched=1, status='pending', error_message=NULL "
                "WHERE product_id=? AND channel='coupang'", (pid,))
        res = coupang_lister.list_product(pid)
        print(f"pid={pid} 복구:", {k: res.get(k) for k in ("ok", "skip", "error")})
        if res.get("ok") and isinstance(res.get("result"), dict):
            print("   new cpid:", res["result"].get("sellerProductId"))


if __name__ == "__main__":
    main(sys.argv[1:])
