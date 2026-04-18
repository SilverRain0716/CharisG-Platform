"""PA SmartStore 속성 수정 — 카테고리별 필수 속성 조회/AI 추론/저장."""
import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.naver_commerce_service import (
    get_product, _get_token, _SESSION, BASE,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pa/smartstore/attributes", tags=["pa-smartstore-attributes"])


# ── 네이버 카테고리 속성 조회 헬퍼 ─────────────────

def _naver_get_attributes(category_id: str) -> list[dict]:
    """카테고리별 속성 목록 조회."""
    token = _get_token()
    if not token:
        return []
    r = _SESSION.get(
        BASE + "/v1/product-attributes/attributes?categoryId=" + category_id,
        headers={"Authorization": f"Bearer {token}"}, timeout=15,
    )
    if r is None or r.status_code >= 400:
        return []
    return r.json()


_ATTR_VALUES_CACHE: dict[str, list[dict]] = {}


def _naver_get_all_attribute_values(category_id: str) -> list[dict]:
    """카테고리의 전체 속성값 목록 (캐시)."""
    if category_id in _ATTR_VALUES_CACHE:
        return _ATTR_VALUES_CACHE[category_id]
    token = _get_token()
    if not token:
        return []
    r = _SESSION.get(
        BASE + f"/v1/product-attributes/attribute-values?categoryId={category_id}",
        headers={"Authorization": f"Bearer {token}"}, timeout=15,
    )
    if r is None or r.status_code >= 400:
        return []
    values = r.json()
    _ATTR_VALUES_CACHE[category_id] = values
    return values


def _naver_get_attribute_values(attribute_seq: int, category_id: str) -> list[dict]:
    """속성의 선택 가능한 값 목록 (전체에서 attributeSeq 필터)."""
    all_values = _naver_get_all_attribute_values(category_id)
    return [v for v in all_values if v["attributeSeq"] == attribute_seq]


# ── 속성 미입력 상품 목록 ──────────────────────────

@router.get("/pending")
def list_pending(user: dict = Depends(current_user)):
    """속성 수정이 필요한 상품 (attributes_updated_at IS NULL, listed)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.product_id, l.channel_product_id, p.title_ko, p.category_path,
                      p.attributes_updated_at
               FROM listings_pa l JOIN products p ON p.id = l.product_id
               WHERE l.channel=? AND l.status='listed'
               AND l.channel_product_id IS NOT NULL AND l.channel_product_id != ''
               AND p.attributes_updated_at IS NULL
               ORDER BY l.product_id""",
            ("smartstore",),
        ).fetchall()
        total_listed = conn.execute(
            "SELECT COUNT(*) c FROM listings_pa WHERE channel='smartstore' AND status='listed'"
        ).fetchone()["c"]
        total_done = conn.execute(
            """SELECT COUNT(*) c FROM listings_pa l JOIN products p ON p.id=l.product_id
               WHERE l.channel='smartstore' AND l.status='listed'
               AND p.attributes_updated_at IS NOT NULL"""
        ).fetchone()["c"]
    return {
        "items": [dict(r) for r in rows],
        "pending": len(rows),
        "done": total_done,
        "total": total_listed,
    }


# ── 단일 상품 속성 조회 ──────────────────────────

@router.get("/{product_id}")
def get_attributes(product_id: int, user: dict = Depends(current_user)):
    """상품의 카테고리 속성 + 현재값 + 가능한 값 반환."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT p.id, p.category_path, p.title_ko, p.title_en, p.description_ko,
                      l.channel_product_id
               FROM products p JOIN listings_pa l ON l.product_id=p.id
               WHERE p.id=? AND l.channel='smartstore'""",
            (product_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "상품 없음")

    category_id = row["category_path"]
    cpid = row["channel_product_id"]
    if not category_id or not category_id.isdigit():
        raise HTTPException(400, "카테고리 ID 없음")

    # 네이버에서 현재 속성값 조회
    current_attrs = []
    if cpid:
        naver_data = get_product(cpid)
        if naver_data:
            current_attrs = (
                naver_data.get("originProduct", {})
                .get("detailAttribute", {})
                .get("productAttributes") or []
            )

    # 카테고리 속성 목록 + 각 값
    attrs_meta = _naver_get_attributes(category_id)
    result = []
    for attr in attrs_meta:
        values = _naver_get_attribute_values(attr["attributeSeq"], category_id)
        # 현재 입력된 값 찾기
        current = [a for a in current_attrs if a["attributeSeq"] == attr["attributeSeq"]]
        current_value_seq = current[0]["attributeValueSeq"] if current else None
        result.append({
            "attributeSeq": attr["attributeSeq"],
            "attributeName": attr["attributeName"],
            "type": attr["attributeClassificationType"],
            "required": attr["attributeType"] == "PRIMARY",
            "currentValueSeq": current_value_seq,
            "values": [
                {"seq": v["attributeValueSeq"], "name": v["minAttributeValue"]}
                for v in values
            ],
        })

    return {
        "product_id": product_id,
        "title": row["title_ko"],
        "category_id": category_id,
        "attributes": result,
    }


# ── AI 추론 ──────────────────────────────────

@router.post("/{product_id}/infer")
async def infer_attributes(product_id: int, user: dict = Depends(current_user)):
    """AI로 상품 속성값 추론."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT p.id, p.category_path, p.title_ko, p.title_en,
                      p.description_ko, p.description_en
               FROM products p WHERE p.id=?""",
            (product_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "상품 없음")

    category_id = row["category_path"]
    title = row["title_ko"] or row["title_en"] or ""
    desc = row["description_ko"] or row["description_en"] or ""

    # 속성 목록 + 값 가져오기
    attrs_meta = _naver_get_attributes(category_id)
    attrs_with_values = []
    for attr in attrs_meta:
        values = _naver_get_attribute_values(attr["attributeSeq"], category_id)
        # minAttributeValue가 없거나 list인 경우 필터링
        safe_values = [
            v for v in values
            if v.get("minAttributeValue") and isinstance(v["minAttributeValue"], str)
        ]
        value_names = [v["minAttributeValue"] for v in safe_values]
        attrs_with_values.append({
            "seq": attr["attributeSeq"],
            "name": attr["attributeName"],
            "type": attr["attributeClassificationType"],
            "options": value_names,
            "values_map": {v["minAttributeValue"]: v["attributeValueSeq"] for v in safe_values},
        })

    if not attrs_with_values:
        return {"product_id": product_id, "inferred": []}

    # Gemini 프롬프트 구성
    attrs_desc = "\n".join(
        f"- {a['name']} ({a['type']}): [{', '.join(a['options'][:20])}]"
        for a in attrs_with_values
    )
    prompt = f"""네이버 쇼핑 상품의 카테고리 속성을 추론하세요.

상품명: {title}
설명: {desc[:300]}

규칙:
1. 반드시 주어진 선택지 중에서만 고르세요.
2. 상품명과 설명에서 합리적으로 유추할 수 있으면 적극적으로 선택하세요.
3. 예: "티셔츠"→소매기장은 "반팔", 네크라인은 "라운드넥"으로 추론 가능합니다.
4. 정말로 판단 불가능한 경우에만 "기타" 또는 "해당없음"을 선택하세요.
5. "기타"보다는 가장 일반적인 값을 우선 선택하세요.

속성 목록:
{attrs_desc}

JSON으로 응답:
{{"results": [{{"name": "속성명", "selected": "선택한 값"}}]}}
"""

    from backend_shared.ai.service import _call_gemini
    try:
        ai_result = await asyncio.to_thread(_call_gemini, prompt)
    except Exception as e:
        logger.error(f"AI 추론 실패 product {product_id}: {e}")
        raise HTTPException(502, f"AI 추론 실패: {e}")

    # AI 응답 파싱
    try:
        import re
        json_match = re.search(r'\{[\s\S]*\}', ai_result)
        parsed = json.loads(json_match.group()) if json_match else {}
        ai_selections = parsed.get("results", [])
    except (json.JSONDecodeError, AttributeError):
        ai_selections = []

    # AI 선택을 attributeValueSeq로 매핑
    inferred = []
    for attr_info in attrs_with_values:
        ai_match = next((s for s in ai_selections if s.get("name") == attr_info["name"]), None)
        selected_name = ai_match.get("selected") if ai_match else None
        # AI가 list로 반환하는 경우 첫 번째 값 사용
        if isinstance(selected_name, list):
            selected_name = selected_name[0] if selected_name else None
        if selected_name and not isinstance(selected_name, str):
            selected_name = str(selected_name)
        selected_seq = attr_info["values_map"].get(selected_name) if selected_name else None

        # 정확 매치 실패 시 부분 매치 시도
        if not selected_seq and selected_name:
            for vname, vseq in attr_info["values_map"].items():
                if selected_name in vname or vname in selected_name:
                    selected_seq = vseq
                    selected_name = vname
                    break

        inferred.append({
            "attributeSeq": attr_info["seq"],
            "attributeName": attr_info["name"],
            "inferredValue": selected_name,
            "inferredValueSeq": selected_seq,
            "options": [{"seq": attr_info["values_map"][o], "name": o} for o in attr_info["options"]],
        })

    return {"product_id": product_id, "inferred": inferred}


# ── 속성 저장 ──────────────────────────────────

class SaveAttributesBody(BaseModel):
    attributes: list[dict]  # [{"attributeSeq": int, "attributeValueSeq": int}, ...]


@router.put("/{product_id}")
def save_attributes(product_id: int, body: SaveAttributesBody, user: dict = Depends(current_user)):
    """상품 속성을 네이버에 저장."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT l.channel_product_id FROM listings_pa l
               WHERE l.product_id=? AND l.channel='smartstore'""",
            (product_id,),
        ).fetchone()
    if not row or not row["channel_product_id"]:
        raise HTTPException(404, "리스팅 없음")

    cpid = row["channel_product_id"]
    naver_data = get_product(cpid)
    if not naver_data:
        raise HTTPException(502, "네이버 상품 조회 실패")

    # productAttributes 설정
    valid_attrs = [
        {"attributeSeq": a["attributeSeq"], "attributeValueSeq": a["attributeValueSeq"]}
        for a in body.attributes
        if a.get("attributeValueSeq")
    ]
    naver_data["originProduct"]["detailAttribute"]["productAttributes"] = valid_attrs

    token = _get_token()
    if not token:
        raise HTTPException(502, "네이버 토큰 발급 실패")

    r = _SESSION.put(
        BASE + f"/v2/products/origin-products/{cpid}",
        json=naver_data,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if r is None or r.status_code >= 400:
        err = r.text[:300] if r is not None else "no-response"
        raise HTTPException(502, f"네이버 저장 실패: {err}")

    # DB에 수정 완료 기록
    with get_db() as conn:
        conn.execute(
            "UPDATE products SET attributes_updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (product_id,),
        )

    return {"ok": True, "saved": len(valid_attrs)}


# ── 일괄 AI 추론 + 저장 ──────────────────────────

class BatchInferBody(BaseModel):
    product_ids: list[int] | None = None
    auto_save: bool = False
    limit: int = 20


@router.post("/batch-infer")
async def batch_infer(body: BatchInferBody, user: dict = Depends(current_user)):
    """여러 상품 일괄 AI 추론 (+ auto_save 시 자동 저장)."""
    if body.product_ids:
        pids = body.product_ids[:body.limit]
    else:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT p.id FROM products p
                   JOIN listings_pa l ON l.product_id=p.id
                   WHERE l.channel='smartstore' AND l.status='listed'
                   AND l.channel_product_id IS NOT NULL
                   AND p.attributes_updated_at IS NULL
                   ORDER BY p.id LIMIT ?""",
                (body.limit,),
            ).fetchall()
        pids = [r["id"] for r in rows]

    if not pids:
        return {"processed": 0, "results": []}

    results = []
    for pid in pids:
        try:
            infer_result = await infer_attributes(pid, user)
            inferred = infer_result.get("inferred", [])

            if body.auto_save and inferred:
                attrs_to_save = [
                    {"attributeSeq": a["attributeSeq"], "attributeValueSeq": a["inferredValueSeq"]}
                    for a in inferred if a.get("inferredValueSeq")
                ]
                if attrs_to_save:
                    try:
                        save_result = save_attributes(
                            pid, SaveAttributesBody(attributes=attrs_to_save), user
                        )
                        results.append({"product_id": pid, "ok": True, "saved": len(attrs_to_save)})
                    except Exception as e:
                        results.append({"product_id": pid, "ok": False, "error": str(e)})
                else:
                    results.append({"product_id": pid, "ok": False, "error": "추론된 값 없음"})
            else:
                results.append({
                    "product_id": pid, "ok": True, "inferred": inferred, "saved": False
                })
        except Exception as e:
            results.append({"product_id": pid, "ok": False, "error": str(e)})

    return {"processed": len(results), "results": results}


# ── 카테고리별 속성 메타 캐시 ─────────────────────────
_ATTRS_META_CACHE: dict[str, list[dict]] = {}


def _get_attrs_with_values(category_id: str) -> list[dict]:
    """카테고리별 속성 + 값 목록 (캐시). Gemini 프롬프트 구성에 재사용."""
    if category_id in _ATTRS_META_CACHE:
        return _ATTRS_META_CACHE[category_id]
    attrs_meta = _naver_get_attributes(category_id)
    result = []
    for attr in attrs_meta:
        values = _naver_get_attribute_values(attr["attributeSeq"], category_id)
        safe_values = [
            v for v in values
            if v.get("minAttributeValue") and isinstance(v["minAttributeValue"], str)
        ]
        value_names = [v["minAttributeValue"] for v in safe_values]
        result.append({
            "seq": attr["attributeSeq"],
            "name": attr["attributeName"],
            "type": attr["attributeClassificationType"],
            "options": value_names,
            "values_map": {v["minAttributeValue"]: v["attributeValueSeq"] for v in safe_values},
        })
    _ATTRS_META_CACHE[category_id] = result
    return result


# ── 멀티 상품 1회 Gemini 추론 ──────────────────────

async def _infer_batch_same_category(
    products: list[dict], attrs_with_values: list[dict]
) -> dict[int, list[dict]]:
    """같은 카테고리의 여러 상품을 Gemini 1회 호출로 추론.

    Returns: {product_id: [{"name": ..., "selected": ...}, ...], ...}
    """
    if not products or not attrs_with_values:
        return {}

    attrs_desc = "\n".join(
        f"- {a['name']} ({a['type']}): [{', '.join(a['options'][:20])}]"
        for a in attrs_with_values
    )
    products_desc = "\n".join(
        f"[상품 {p['id']}] {(p.get('title_ko') or p.get('title_en') or '')[:80]}"
        for p in products
    )
    prompt = f"""네이버 쇼핑 상품의 카테고리 속성을 추론하세요. 여러 상품을 한번에 처리합니다.

상품 목록:
{products_desc}

규칙:
1. 반드시 주어진 선택지 중에서만 고르세요.
2. 상품명에서 합리적으로 유추할 수 있으면 적극적으로 선택하세요.
3. "기타"보다는 가장 일반적인 값을 우선 선택하세요.

속성 목록:
{attrs_desc}

JSON으로 응답 (각 상품별):
{{"products": [{{"id": 상품ID, "results": [{{"name": "속성명", "selected": "선택한 값"}}]}}]}}
"""
    from backend_shared.ai.service import _call_gemini
    try:
        ai_result = await asyncio.to_thread(_call_gemini, prompt)
    except Exception as e:
        logger.error(f"[batch-infer] Gemini 실패: {e}")
        return {}

    import re
    try:
        json_match = re.search(r'\{[\s\S]*\}', ai_result)
        parsed = json.loads(json_match.group()) if json_match else {}
        ai_products = parsed.get("products", [])
    except (json.JSONDecodeError, AttributeError):
        return {}

    # 결과를 product_id별로 매핑
    result: dict[int, list[dict]] = {}
    for ap in ai_products:
        raw_id = ap.get("id")
        selections = ap.get("results", [])
        if raw_id is None:
            continue
        # Gemini가 "상품 123" 형태로 반환하는 경우 처리
        try:
            import re as _re
            pid = int(_re.sub(r'[^0-9]', '', str(raw_id)))
        except (ValueError, TypeError):
            continue
        if pid:
            result[pid] = selections
    return result


def _map_ai_to_attrs(
    ai_selections: list[dict], attrs_with_values: list[dict]
) -> list[dict]:
    """AI 선택을 attributeSeq/ValueSeq 쌍으로 매핑."""
    mapped = []
    for attr_info in attrs_with_values:
        ai_match = next((s for s in ai_selections if s.get("name") == attr_info["name"]), None)
        selected_name = ai_match.get("selected") if ai_match else None
        if isinstance(selected_name, list):
            selected_name = selected_name[0] if selected_name else None
        if selected_name and not isinstance(selected_name, str):
            selected_name = str(selected_name)
        selected_seq = attr_info["values_map"].get(selected_name) if selected_name else None
        if not selected_seq and selected_name:
            for vname, vseq in attr_info["values_map"].items():
                if selected_name in vname or vname in selected_name:
                    selected_seq = vseq
                    break
        if selected_seq:
            mapped.append({"attributeSeq": attr_info["seq"], "attributeValueSeq": selected_seq})
    return mapped


# ── 전체 일괄 처리 (백그라운드, 카테고리별 배치) ──────

_BATCH_ALL_STATUS: dict = {"running": False, "processed": 0, "errors": 0, "total": 0, "current_id": None}
BATCH_GROUP_SIZE = 10  # 같은 카테고리 상품 N개를 Gemini 1회로 처리


@router.post("/batch-all")
async def batch_all(user: dict = Depends(current_user)):
    """전체 미처리 상품 AI 추론 + 자동 저장 (백그라운드, 카테고리별 배치)."""
    if _BATCH_ALL_STATUS["running"]:
        return {"ok": False, "error": "이미 실행 중", **_BATCH_ALL_STATUS}

    with get_db() as conn:
        rows = conn.execute(
            """SELECT p.id, p.category_path, p.title_ko, p.title_en
               FROM products p
               JOIN listings_pa l ON l.product_id=p.id
               WHERE l.channel='smartstore' AND l.status='listed'
               AND l.channel_product_id IS NOT NULL
               AND p.attributes_updated_at IS NULL
               ORDER BY p.category_path, p.id""",
        ).fetchall()
    products = [dict(r) for r in rows]

    if not products:
        return {"ok": True, "processed": 0, "total": 0}

    # 카테고리별 그룹핑
    from itertools import groupby
    groups: list[list[dict]] = []
    for _, grp in groupby(products, key=lambda p: p["category_path"]):
        cat_products = list(grp)
        for i in range(0, len(cat_products), BATCH_GROUP_SIZE):
            groups.append(cat_products[i:i + BATCH_GROUP_SIZE])

    total = len(products)
    _BATCH_ALL_STATUS.update(running=True, processed=0, errors=0, total=total, current_id=None)

    async def _run():
        for gi, group in enumerate(groups):
            if gi > 0:
                await asyncio.sleep(1.5)

            cat_id = group[0]["category_path"]
            _BATCH_ALL_STATUS["current_id"] = group[0]["id"]

            try:
                attrs_with_values = _get_attrs_with_values(cat_id)
                if not attrs_with_values:
                    _BATCH_ALL_STATUS["processed"] += len(group)
                    continue

                # Gemini 1회로 그룹 전체 추론
                ai_results = await _infer_batch_same_category(group, attrs_with_values)

                for p in group:
                    pid = p["id"]
                    ai_sel = ai_results.get(pid, [])
                    mapped = _map_ai_to_attrs(ai_sel, attrs_with_values)
                    if mapped:
                        try:
                            save_attributes(pid, SaveAttributesBody(attributes=mapped), user)
                            _BATCH_ALL_STATUS["processed"] += 1
                        except Exception as e:
                            logger.warning(f"[batch-all] product {pid} 네이버 저장 실패: {e}")
                            _BATCH_ALL_STATUS["errors"] += 1
                    else:
                        _BATCH_ALL_STATUS["processed"] += 1

            except Exception as e:
                logger.warning(f"[batch-all] group {[p['id'] for p in group]} 실패: {e}")
                _BATCH_ALL_STATUS["errors"] += len(group)

        _BATCH_ALL_STATUS["running"] = False
        _BATCH_ALL_STATUS["current_id"] = None

    asyncio.create_task(_run())
    return {"ok": True, "total": total, "groups": len(groups)}


@router.get("/batch-all/status")
def batch_all_status(user: dict = Depends(current_user)):
    """전체 일괄 처리 진행률 조회."""
    return _BATCH_ALL_STATUS
