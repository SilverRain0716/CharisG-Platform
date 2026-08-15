"""load_elevenst_categories.py — 11번가 카테고리 트리 적재.

상품등록의 `dispCtgrNo`(카테고리번호)를 채우려면 11번가 카테고리 체계가 로컬에
있어야 한다. 쿠팡은 coupang_categories 를 갖고 있는데 11번가는 테이블조차 없었다.

원본: GET /rest/cateservice/category — ★인증 불필요, 응답 8.8MB(15,295 노드).
      루트 <categorys> 아래 <category> 가 평면으로 깔리고 parentDispNo 로 트리를 만든다.

노드 필드:
    depth / dispNm(이름) / dispNo(번호) / parentDispNo(부모)
    leafYn(말단 여부 — 등록은 말단에만 가능)
    gblDlvYn(해외배송 가능 여부) / engDispYn

★루프에서 category_tree() 를 부르지 말 것. 8.8MB 라 호출 비용이 크다. 이 스크립트로
  1회 적재하고 이후에는 DB 를 본다.

사용:
    PYTHONPATH=<repo> .venv/bin/python -m backend.purchase.scripts.load_elevenst_categories [--dry-run]
"""
import argparse
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))

import logging

from backend.purchase.database import get_db
from backend.purchase.services.elevenst_service import category_tree

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("11st-cat")

KST = timezone(timedelta(hours=9))

DDL = """
CREATE TABLE IF NOT EXISTS elevenst_categories (
    disp_no        TEXT PRIMARY KEY,          -- dispCtgrNo 로 그대로 쓰는 값
    name           TEXT NOT NULL,
    depth          INTEGER,
    parent_disp_no TEXT,
    is_leaf        INTEGER NOT NULL DEFAULT 0,  -- 등록은 말단(leaf)에만 가능
    global_dlv     INTEGER NOT NULL DEFAULT 0,  -- 해외배송 가능
    eng_disp       INTEGER NOT NULL DEFAULT 0,
    full_path      TEXT,                        -- '여행/숙박 > ...' 조립값(매핑·검색용)
    synced_at      TEXT
)
"""
IDX = [
    "CREATE INDEX IF NOT EXISTS idx_11st_cat_parent ON elevenst_categories(parent_disp_no)",
    "CREATE INDEX IF NOT EXISTS idx_11st_cat_leaf ON elevenst_categories(is_leaf, global_dlv)",
    "CREATE INDEX IF NOT EXISTS idx_11st_cat_name ON elevenst_categories(name)",
]

NS = "{http://skt.tmall.business.openapi.spring.service.client.domain/}"


def _t(node, tag: str) -> str:
    """★네임스페이스가 <categorys>/<category> 에만 붙고 그 안쪽 필드에는 없다.
    NS 를 붙여 찾으면 전부 None 이 나온다 — 둘 다 시도한다."""
    el = node.find(tag)
    if el is None:
        el = node.find(NS + tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logger.info("카테고리 트리 조회 중 (8.8MB, 1~2분)")
    root = category_tree()
    nodes = list(root)
    logger.info("노드 %d개", len(nodes))

    rows = {}
    for n in nodes:
        no = _t(n, "dispNo")
        if not no:
            continue
        rows[no] = {
            "disp_no": no,
            "name": _t(n, "dispNm"),
            "depth": int(_t(n, "depth") or 0),
            "parent": _t(n, "parentDispNo") or None,
            "leaf": 1 if _t(n, "leafYn") == "Y" else 0,
            "gbl": 1 if _t(n, "gblDlvYn") == "Y" else 0,
            "eng": 1 if _t(n, "engDispYn") == "Y" else 0,
        }

    # 전체 경로 조립 — 매핑할 때 이름만으로는 중복이 많아 경로가 있어야 고를 수 있다.
    def path_of(no: str, seen=None) -> str:
        seen = seen or set()
        r = rows.get(no)
        if not r or no in seen:
            return ""
        seen.add(no)
        parent = r["parent"]
        if parent and parent != "0" and parent in rows:
            head = path_of(parent, seen)
            return f"{head} > {r['name']}" if head else r["name"]
        return r["name"]

    for no, r in rows.items():
        r["path"] = path_of(no)

    leaf = sum(1 for r in rows.values() if r["leaf"])
    gbl_leaf = sum(1 for r in rows.values() if r["leaf"] and r["gbl"])
    depths = {}
    for r in rows.values():
        depths[r["depth"]] = depths.get(r["depth"], 0) + 1
    logger.info("말단 %d개 · 그중 해외배송 가능 %d개 · 깊이 분포 %s",
                leaf, gbl_leaf, dict(sorted(depths.items())))

    if args.dry_run:
        logger.info("DRY-RUN — 표본 5개")
        for r in list(rows.values())[:5]:
            logger.info("  %s d%s leaf=%s gbl=%s  %s", r["disp_no"], r["depth"],
                        r["leaf"], r["gbl"], r["path"][:70])
        return

    stamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(DDL)
        for sql in IDX:
            conn.execute(sql)
        conn.executemany(
            """INSERT INTO elevenst_categories
                 (disp_no, name, depth, parent_disp_no, is_leaf, global_dlv, eng_disp, full_path, synced_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(disp_no) DO UPDATE SET
                 name=excluded.name, depth=excluded.depth,
                 parent_disp_no=excluded.parent_disp_no, is_leaf=excluded.is_leaf,
                 global_dlv=excluded.global_dlv, eng_disp=excluded.eng_disp,
                 full_path=excluded.full_path, synced_at=excluded.synced_at""",
            [(r["disp_no"], r["name"], r["depth"], r["parent"], r["leaf"],
              r["gbl"], r["eng"], r["path"], stamp) for r in rows.values()],
        )
    logger.info("적재 완료 %d행", len(rows))


if __name__ == "__main__":
    main()
