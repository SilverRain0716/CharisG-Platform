"""1회 실행: 레거시 categories.db → PA naver_categories + 임베딩 생성."""
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from backend.purchase.services.category_rag import sync_from_legacy, build_embeddings


def main():
    logger.info("[1/2] 레거시 naver_categories 동기화")
    n = sync_from_legacy()
    logger.info(f"  동기화 완료: {n}행")

    logger.info("[2/2] 임베딩 생성 (없는 행만)")
    result = build_embeddings(batch_size=100)
    logger.info(f"  임베딩 결과: {result}")

    if result.get("failed", 0) > 0:
        logger.warning(f"임베딩 실패 {result['failed']}행 — 재실행 시 남은 항목만 처리")
        sys.exit(1)


if __name__ == "__main__":
    main()
