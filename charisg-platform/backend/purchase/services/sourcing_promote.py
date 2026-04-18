"""sourcing_candidates → products 일괄 이관 (SP-API 보강 포함).

워크플로우: 사용자가 Sourcing 페이지에서 "상품관리로 전체 이관" 버튼을 누르면
남아있는 모든 sourcing_candidates 행을 products 에 INSERT 한 뒤
sourcing_candidates 테이블을 비운다.

SP-API 보강: promote 시 각 ASIN에 대해 SP-API로 정확한 상품정보를 수집하여
title_en, description_en, brand, images_json 을 채운다.
시트에서 가져온 title이 인증 배지 설명 등으로 오염된 경우를 방지한다.

주의 — FK 제약 우회:
  products.sourcing_id 는 sourcing_candidates(id) 를 REFERENCES 하고
  database.get_db() 는 PRAGMA foreign_keys=ON 을 건다. 따라서 같은 트랜잭션에서
  INSERT 후 바로 부모 DELETE 하면 자식(방금 넣은 products 행)을 남긴 채 부모를
  지우려다 FOREIGN KEY constraint failed 로 깨진다.

  사용자 요구는 '이관 후 products.sourcing_id 를 이력 포인터로 남긴다' 이므로
  sourcing_id 를 NULL 로 지우는 건 설계 위반이다. 대신 이 오퍼레이션 전용
  커넥션을 열어 foreign_keys=OFF 로 두고 INSERT+DELETE 를 원자적으로 처리한다.
  DELETE 후 products.sourcing_id 는 부모가 사라진 dangling 포인터가 되지만,
  products.asin + products.created_at 으로 충분히 추적 가능하다.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

from backend.purchase.database import DB_PATH

logger = logging.getLogger(__name__)

# SP-API rate limit: 2 req/sec
_SP_API_INTERVAL = 0.55


def _enrich_from_sp_api(asin: str) -> dict:
    """SP-API로 상품정보 보강. 실패 시 빈 dict 반환."""
    try:
        from backend.purchase.services.image_downloader import fetch_product_info_sp_api
        info = fetch_product_info_sp_api(asin)
        time.sleep(_SP_API_INTERVAL)
        return info
    except Exception as e:
        logger.warning(f"SP-API 보강 실패 ({asin}): {e}")
        return {}


def promote_all() -> dict:
    """sourcing_candidates 의 모든 행 → products INSERT + sourcing_candidates DELETE.

    SP-API로 각 ASIN의 정확한 상품정보(title, description, brand, images)를 수집하여
    시트 데이터를 보강/대체한다.

    Returns:
        {promoted: int, enriched: int, total: int}
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        rows = conn.execute(
            "SELECT id, asin, title, price_usd, image_url FROM sourcing_candidates"
        ).fetchall()

        if not rows:
            return {"promoted": 0, "enriched": 0, "total": 0}

        promoted = 0
        enriched = 0

        for r in rows:
            asin = r["asin"]
            sheet_title = r["title"]
            sheet_image = r["image_url"]
            cost_usd = r["price_usd"]

            # SP-API 보강
            sp = _enrich_from_sp_api(asin) if asin else {}

            # title: SP-API 우선 (시트 데이터가 인증 배지로 오염될 수 있음)
            title_en = sp.get("title") or sheet_title

            # description: SP-API에서만 가져올 수 있음
            description_en = sp.get("description") or ""
            bullet_points = sp.get("bullet_points")
            if not description_en and bullet_points:
                description_en = "\n".join(f"• {bp}" for bp in bullet_points)

            # brand: SP-API에서만
            brand = sp.get("brand") or ""

            # images: SP-API 이미지 (최대 15장) > 시트 이미지 (1장)
            sp_images = sp.get("images", [])
            if sp_images:
                images_json = json.dumps(sp_images, ensure_ascii=False)
            elif sheet_image:
                images_json = json.dumps([sheet_image], ensure_ascii=False)
            else:
                images_json = None

            conn.execute(
                """INSERT INTO products
                   (sourcing_id, business_model, asin, title_en, description_en,
                    brand, cost_usd, images_json, status)
                   VALUES (?, 'purchase', ?, ?, ?, ?, ?, ?, 'draft')""",
                (r["id"], asin, title_en, description_en, brand,
                 cost_usd, images_json),
            )
            promoted += 1
            if sp:
                enriched += 1

        conn.execute("DELETE FROM sourcing_candidates")
        conn.commit()

        logger.info(f"[promote] {promoted}건 이관, {enriched}건 SP-API 보강")
        return {"promoted": promoted, "enriched": enriched, "total": len(rows)}

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
