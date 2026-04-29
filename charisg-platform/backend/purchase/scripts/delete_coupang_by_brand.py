"""
delete_coupang_by_brand.py — 쿠팡 리스팅 브랜드 단위 일괄 삭제.

쿠팡 유통경로 소명(정품 게이팅) 대응:
    거래내역이 없어 소명 불가한 상품을 브랜드 키워드로 매칭해 전수 삭제.

매칭 규칙:
    - title_en 대문자 기준 단어 경계 매칭 (영문 브랜드)
    - title_ko 부분 문자열 매칭 (한글 브랜드)

동작:
    1. listings_pa(channel='coupang', status IN ('listed','active')) 중 브랜드 매칭
    2. 각각 coupang_service.delete_product(channel_product_id) 호출
    3. 성공 → listings_pa.status='removed' + error_message에 사유 기록

사용:
    python3 -m backend.purchase.scripts.delete_coupang_by_brand \\
        --brands "NIKE,ADIDAS,PUMA,STANLEY,LACOSTE,TITLEIST,CARHARTT,\\
나이키,아디다스,푸마,스탠리,라코스테,타이틀리스트,칼하트" \\
        --dry-run
"""
import argparse
import logging
import re
import sqlite3
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.purchase.services.coupang_service import delete_product, stop_sales

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_BRANDS = (
    "NIKE", "ADIDAS", "PUMA", "STANLEY", "LACOSTE", "TITLEIST", "CARHARTT",
    "나이키", "아디다스", "푸마", "스탠리", "라코스테", "타이틀리스트", "칼하트",
)


def _match_brand(title_en: str, title_ko: str, brands: list[str]) -> str | None:
    en = (title_en or "").upper()
    ko = title_ko or ""
    for kw in brands:
        if not kw:
            continue
        if re.search(r"[A-Za-z]", kw):
            if re.search(rf"\b{re.escape(kw.upper())}\b", en):
                return kw
        else:
            if kw in ko:
                return kw
    return None


def collect_targets(conn, brands: list[str]) -> list[dict]:
    rows = conn.execute(
        """SELECT l.id AS lid, l.product_id, l.channel_product_id, l.status,
                  p.title_en, p.title_ko, p.asin
           FROM listings_pa l JOIN products p ON p.id = l.product_id
           WHERE l.channel='coupang' AND l.status IN ('listed','active')
           ORDER BY l.id"""
    ).fetchall()
    targets = []
    for r in rows:
        matched = _match_brand(r["title_en"], r["title_ko"], brands)
        if matched:
            targets.append({
                "lid": r["lid"],
                "product_id": r["product_id"],
                "channel_product_id": r["channel_product_id"],
                "title": (r["title_ko"] or r["title_en"] or "").strip()[:60],
                "matched_brand": matched,
            })
    return targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brands", type=str, default=",".join(DEFAULT_BRANDS),
                    help="쉼표로 구분한 브랜드 키워드 목록")
    ap.add_argument("--mode", type=str, default="stop", choices=["stop", "delete"],
                    help="stop=판매중지(가역, 기본) / delete=완전삭제(저장중 상태만 가능)")
    ap.add_argument("--dry-run", action="store_true", help="실제 API 호출/DB 갱신 없이 대상만 출력")
    ap.add_argument("--limit", type=int, default=None, help="최대 처리 건수")
    ap.add_argument("--db", type=str, default=None, help="DB 경로 override")
    args = ap.parse_args()

    brands = [b.strip() for b in args.brands.split(",") if b.strip()]
    if not brands:
        logger.error("브랜드 목록이 비어있음")
        return

    db_path = args.db or str(Path(__file__).resolve().parents[1] / "purchase.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    targets = collect_targets(conn, brands)
    if args.limit:
        targets = targets[: args.limit]

    logger.info(f"DB: {db_path}")
    logger.info(f"브랜드: {brands}")
    logger.info(f"삭제 대상: {len(targets)}건")

    if not targets:
        return

    if args.dry_run:
        for t in targets[:30]:
            logger.info(f"  [DRY] lid={t['lid']} pid={t['product_id']} cpid={t['channel_product_id']} "
                        f"brand={t['matched_brand']} | {t['title']}")
        if len(targets) > 30:
            logger.info(f"  ... +{len(targets) - 30}건 더")
        return

    ok = 0
    fail = 0
    skip = 0
    fail_msgs = []
    api_fn = stop_sales if args.mode == "stop" else delete_product
    new_status = "paused" if args.mode == "stop" else "removed"
    label = "판매중지" if args.mode == "stop" else "삭제"

    for i, t in enumerate(targets):
        cpid = t["channel_product_id"]
        if not cpid:
            skip += 1
            # channel_product_id 없으면 쿠팡 API 호출 불가 — DB만 removed 처리
            conn.execute(
                """UPDATE listings_pa SET status='removed',
                   error_message=?, last_synced_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (f"브랜드 게이팅 대응 — cpid 없음 ({t['matched_brand']})", t["lid"]),
            )
            conn.commit()
            continue

        success, err = api_fn(cpid)
        if success:
            ok += 1
            conn.execute(
                """UPDATE listings_pa SET status=?,
                   error_message=?, last_synced_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (new_status, f"브랜드 게이팅 대응 {label} ({t['matched_brand']})", t["lid"]),
            )
            conn.commit()
        else:
            fail += 1
            if len(fail_msgs) < 10:
                fail_msgs.append((cpid, err))

        if (i + 1) % 10 == 0:
            logger.info(f"  progress {i+1}/{len(targets)} ok={ok} fail={fail} skip={skip}")
        time.sleep(0.3)  # 쿠팡 API 과도 호출 회피

    logger.info(f"완료: {label} 성공 {ok}, 실패 {fail}, cpid없음 skip {skip}")
    for cpid, err in fail_msgs:
        logger.warning(f"  {cpid}: {err}")


if __name__ == "__main__":
    main()
