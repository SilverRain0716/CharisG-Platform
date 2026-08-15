"""
sp_api_match_asin.py — CJ 상품 → Amazon ASIN 매칭 CLI.

기존 ASIN 검색 + offer 등록 테스트 스크립트.

실행:
    cd ~/projects/CharisG-Platform
    charisg-platform/.venv/bin/python charisg-platform/scripts/sp_api_match_asin.py 842
    charisg-platform/.venv/bin/python charisg-platform/scripts/sp_api_match_asin.py --batch 20
    charisg-platform/.venv/bin/python charisg-platform/scripts/sp_api_match_asin.py 842 --register
    charisg-platform/.venv/bin/python charisg-platform/scripts/sp_api_match_asin.py 842 --register --confirm
"""
import argparse
import json
import os
import sys

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
sys.path.insert(0, ROOT)

from backend.dropshipping.database import init_db
from backend.dropshipping.services import asin_matching_service, offer_registration_service


def print_candidates(product_id: int, candidates: list[dict]):
    """후보 ASIN 목록 출력."""
    print(f"\n  상품 ID: {product_id}")
    print(f"  후보 {len(candidates)}건:")
    print(f"  {'ASIN':<14} {'Score':>6} {'Verdict':<10} {'TitleSim':>8} {'PriceC':>7}  Title")
    print("  " + "-" * 80)
    for c in candidates:
        print(
            f"  {c['asin']:<14} {c.get('match_score', 0):>6.3f} "
            f"{c.get('match_verdict', ''):<10} {c.get('title_sim', 0):>8.3f} "
            f"{c.get('price_compat', 0):>7.3f}  "
            f"{(c.get('amazon_title') or '')[:50]}"
        )


def cmd_single(args):
    """단일 상품 ASIN 매칭."""
    print(f"\n{'=' * 70}")
    print(f"  ASIN 매칭: product_id={args.product_id}")
    print(f"{'=' * 70}")

    best = asin_matching_service.find_best_match(args.product_id)
    candidates = asin_matching_service.get_candidates(args.product_id)

    print_candidates(args.product_id, candidates)

    if best:
        print(f"\n  >>> 최적 매칭: {best['asin']} "
              f"({best['match_verdict']}, score={best['match_score']:.3f})")
    else:
        print("\n  >>> 매칭 결과 없음")

    return best


def cmd_batch(args):
    """일괄 매칭."""
    print(f"\n{'=' * 70}")
    print(f"  일괄 ASIN 매칭: limit={args.batch}")
    print(f"{'=' * 70}")

    def progress(phase, current, total, message):
        print(f"  [{current}/{total}] {message}")

    result = asin_matching_service.batch_match(
        limit=args.batch,
        progress_cb=progress,
    )

    print(f"\n  결과: 처리={result['processed']}, "
          f"매칭={result['matched']}, 미매칭={result['no_match']}, "
          f"실패={result['failed']}")

    for r in result["results"]:
        if r.get("asin"):
            print(f"    product_id={r['product_id']} → {r['asin']} ({r['match_verdict']})")
        else:
            print(f"    product_id={r['product_id']} → 매칭 없음")


def cmd_register(args):
    """매칭 + offer 등록."""
    best = cmd_single(args)
    if not best:
        print("  매칭 실패 → offer 등록 불가")
        return

    dry_run = not args.confirm
    mode = "VALIDATION_PREVIEW" if dry_run else "PRODUCTION"
    print(f"\n  Offer 등록 ({mode})...")

    result = offer_registration_service.register_offer(
        args.product_id, dry_run=dry_run,
    )

    print(f"\n  결과: ok={result['ok']}")
    print(f"  ASIN: {result.get('asin')}")
    print(f"  SKU:  {result.get('sku')}")
    print(f"  Status: {result.get('status')}")

    if result.get("errors"):
        print(f"\n  ERRORS ({len(result['errors'])}):")
        for e in result["errors"]:
            print(f"    - {e.get('code')}: {e.get('message')}")

    if result.get("warnings"):
        print(f"\n  WARNINGS ({len(result.get('warnings', []))}):")
        for w in result.get("warnings", []):
            print(f"    - {w.get('code')}: {w.get('message')}")

    if result.get("submission_id"):
        print(f"\n  Submission ID: {result['submission_id']}")

    # payload 저장
    if result.get("payload"):
        out = f"/tmp/offer_payload_{args.product_id}.json"
        with open(out, "w") as f:
            json.dump(result["payload"], f, indent=2, ensure_ascii=False)
        print(f"\n  Payload 저장: {out}")


def cmd_summary(_args):
    """파이프라인 현황."""
    summary = asin_matching_service.get_pipeline_summary()
    print(f"\n{'=' * 70}")
    print("  ASIN Pipeline Summary")
    print(f"{'=' * 70}")
    print(f"  필터 통과 상품: {summary['total_filtered']}")
    print(f"  ASIN 매칭 완료: {summary['matched']}")
    print(f"  미매칭:         {summary['unmatched']}")
    print(f"  리스팅 등록:    {summary['listed']}")
    print(f"  활성 상품:      {summary['active']}")


def main():
    parser = argparse.ArgumentParser(description="CJ → Amazon ASIN 매칭 CLI")
    parser.add_argument("product_id", nargs="?", type=int, help="상품 ID")
    parser.add_argument("--batch", type=int, help="일괄 매칭 (상위 N개)")
    parser.add_argument("--register", action="store_true", help="매칭 + offer 등록")
    parser.add_argument("--confirm", action="store_true", help="실제 등록 (dry_run=False)")
    parser.add_argument("--summary", action="store_true", help="파이프라인 현황")

    args = parser.parse_args()

    # DB 초기화 (테이블 생성)
    init_db()

    if args.summary:
        cmd_summary(args)
    elif args.batch:
        cmd_batch(args)
    elif args.product_id and args.register:
        cmd_register(args)
    elif args.product_id:
        cmd_single(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    sys.exit(main() or 0)
