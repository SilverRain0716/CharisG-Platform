"""쿠팡 listed 상품의 searchTags 점진 백필 — daily 호출.

사용법 (EC2):
  cd ~/CharisG-Platform/charisg-platform
  .venv/bin/python -m backend.purchase.scripts.backfill_coupang_search_tags --limit 1000

옵션:
  --limit N   하루 처리 건수 (기본 1000)
  --rate F    초당 요청 (기본 8.0, 쿠팡 한도 10 미만)
  --dry-run   PUT 호출 없이 페이로드만 검증
"""
import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

import os as _os
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(_os.path.join(_os.environ.get('CHARISG_ROOT', '/home/ubuntu/CharisG-Platform/charisg-platform'), '.env'))
from backend.purchase.database import get_db
from backend.purchase.services.coupang_lister import _normalize_search_tags, _extract_brand
from backend.purchase.services.coupang_service import update_product_search_tags, active_account

logger = logging.getLogger(__name__)

# ★2026-07-23: 23시 stop_searchtags.sh 의 pkill(SIGTERM) 로 루프 중단 시에도
#   진행분을 저장하기 위한 협조적 종료 플래그. (예전엔 루프 완주 후에만 마킹해서
#   11,000건을 59분에 못 끝내고 매일 같은 자리를 반복 처리했음)
_STOP = False
FLUSH_EVERY = 200


def _on_term(signum, _frame):
    global _STOP
    _STOP = True
    logger.warning(f"시그널 {signum} 수신 — 진행분 저장 후 종료합니다")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_targets(limit: int) -> list[dict]:
    """listed + synced_at NULL + seo_tags 보유 product 선정 (id ASC).

    ★계정 필터 필수(2026-07-23): 조회/PUT 은 활성계정(COUPANG_ACTIVE)의 자격증명으로
      나가므로, 타계정 소유 상품을 섞으면 전량 실패하고 영구실패로 마킹돼 영영 재시도되지
      않는다. 반드시 active_account() 로 소유계정을 일치시킬 것.
    """
    acct = active_account()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.seo_tags, p.title_ko, lp.channel_product_id
            FROM products p
            JOIN listings_pa lp ON lp.product_id=p.id
              AND lp.channel='coupang' AND lp.status='listed'
              AND lp.coupang_account=?
              AND lp.channel_product_id IS NOT NULL AND lp.channel_product_id != ''
            WHERE p.coupang_search_tags_synced_at IS NULL
              AND p.seo_tags IS NOT NULL AND p.seo_tags != '' AND p.seo_tags != '[]'
            ORDER BY p.id ASC
            LIMIT ?
            """,
            (acct, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_synced(product_ids: list[int], when: str) -> None:
    """100개 단위 batch UPDATE — CSV-merge 패턴 (DB lock 회피)."""
    if not product_ids:
        return
    with get_db() as conn:
        for i in range(0, len(product_ids), 100):
            chunk = product_ids[i:i + 100]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"UPDATE products SET coupang_search_tags_synced_at=? "
                f"WHERE id IN ({placeholders})",
                (when, *chunk),
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--rate", type=float, default=8.0, help="요청/초")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    targets = fetch_targets(args.limit)
    if not targets:
        logger.info("백필 대상 0건 — 종료")
        return 0

    logger.info(
        f"백필 대상: {len(targets)}개, rate={args.rate}/s, dry_run={args.dry_run}"
    )

    sleep_per = 1.0 / max(args.rate, 1.0)
    ok_ids: list[int] = []
    err_count = 0
    skipped = 0
    perm_fail_ids = []  # 영구실패(그룹자식·삭제) → 마킹해 재시도 안 함
    pending_mark: list[int] = []  # 아직 DB 반영 안 된 처리분 (주기 flush 대상)
    marked_total = 0
    started = time.monotonic()

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    def _flush() -> int:
        """누적분을 DB 반영하고 버퍼를 비운다. 반영 건수 반환."""
        nonlocal marked_total
        if args.dry_run or not pending_mark:
            return 0
        n = len(pending_mark)
        mark_synced(list(pending_mark), _now_iso())
        pending_mark.clear()
        marked_total += n
        return n

    for i, t in enumerate(targets, 1):
        product_id = t["id"]
        seller_pid = str(t["channel_product_id"])
        # brand 컬럼은 부정확한 케이스 다수 — title_ko 첫 영문 단어만 사용 (신규 listing 과 동일)
        derived_brand = _extract_brand(t["title_ko"] or "")
        tags = _normalize_search_tags(t["seo_tags"], derived_brand)
        if not tags:
            logger.warning(f"product {product_id} normalized tags 비어있음 — skip")
            skipped += 1
            continue
        try:
            ok, msg = update_product_search_tags(
                seller_pid, tags, dry_run=args.dry_run
            )
        except Exception as e:
            logger.exception(f"product {product_id} 예외")
            ok, msg = False, str(e)

        if ok:
            ok_ids.append(product_id)
            pending_mark.append(product_id)
            if i % 100 == 0 or i == len(targets):
                logger.info(
                    f"진행 {i}/{len(targets)} (성공 {len(ok_ids)}, 실패 {err_count})"
                )
        else:
            err_count += 1
            logger.warning(
                f"product {product_id} (seller_pid={seller_pid}) 실패: {msg}"
            )
            # 그룹자식(옵션은 마스터가 searchTags 커버)·삭제 등 영구실패는 마킹해 재시도 제외.
            # 일시("진행중")만 다음 사이클 재시도.
            if msg and ("진행중" not in str(msg)):
                perm_fail_ids.append(product_id)
                pending_mark.append(product_id)

        # 주기 flush — 중간에 죽어도 진행분 보존
        if len(pending_mark) >= FLUSH_EVERY:
            n = _flush()
            if n:
                logger.info(f"중간 저장 {n}건 (누적 {marked_total})")

        if _STOP:
            n = _flush()
            logger.warning(
                f"중단 요청 — {i}/{len(targets)} 에서 종료 (마지막 저장 {n}건)"
            )
            break

        time.sleep(sleep_per)

    _flush()  # 잔여분

    elapsed = time.monotonic() - started
    logger.info(
        f"완료: 성공 {len(ok_ids)} / 실패 {err_count} / skip {skipped} / "
        f"DB반영 {marked_total} / 소요 {elapsed:.1f}s"
    )
    # 성공 0건 + 실패 다수면 비정상 종료
    return 0 if (len(ok_ids) > 0 or len(targets) == skipped) else 1


if __name__ == "__main__":
    sys.exit(main())
