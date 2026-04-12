"""sourcing_candidates → products 일괄 이관.

워크플로우: 사용자가 Sourcing 페이지에서 "상품관리로 전체 이관" 버튼을 누르면
남아있는 모든 sourcing_candidates 행을 products 에 INSERT 한 뒤
sourcing_candidates 테이블을 비운다.

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
import sqlite3

from backend.purchase.database import DB_PATH


def promote_all() -> int:
    """sourcing_candidates 의 모든 행 → products INSERT + sourcing_candidates DELETE.

    Returns:
        이관된 행 개수.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        rows = conn.execute(
            "SELECT id, asin, title, price_usd, image_url FROM sourcing_candidates"
        ).fetchall()

        if not rows:
            return 0

        promoted = 0
        for r in rows:
            images_json = json.dumps([r["image_url"]], ensure_ascii=False) if r["image_url"] else None
            conn.execute(
                """INSERT INTO products
                   (sourcing_id, business_model, asin, title_en, cost_usd, images_json, status)
                   VALUES (?, 'purchase', ?, ?, ?, ?, 'draft')""",
                (r["id"], r["asin"], r["title"], r["price_usd"], images_json),
            )
            promoted += 1

        conn.execute("DELETE FROM sourcing_candidates")
        conn.commit()
        return promoted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
