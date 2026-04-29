"""
groups.py — Phase 3 옵션 그룹 (variation_groups) UI API.

GET    /api/pa/groups                    — group list (children 카운트, theme, brand 등)
GET    /api/pa/groups/{parent_asin}      — 특정 group 상세 (master + children + 분리 미리보기)
POST   /api/pa/groups/{parent_asin}/save-rule  — 사용자 차원 변경 → category_split_rules 누적
GET    /api/pa/groups/stats              — 한도 초과·검토 필요 group 카운트
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.purchase.database import get_db
from backend.purchase.auth import current_user
from backend.purchase.services.variation import (
    CHANNEL_LIMIT, load_group, determine_primary_dim, auto_split,
    calculate_group_pricing, save_category_rule, korean_label,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pa/groups", tags=["pa-groups"])


@router.get("")
def list_groups(
    over_limit: Optional[bool] = Query(None, description="채널 한도 초과 여부 필터"),
    channel: str = Query("coupang"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(current_user),
):
    """variation_groups list — 검색·필터·페이징."""
    LIMIT_VAL = CHANNEL_LIMIT.get(channel, 30)
    where = ["1=1"]
    params: list = []
    if over_limit is True:
        where.append("child_count > ?")
        params.append(LIMIT_VAL)
    elif over_limit is False:
        where.append("child_count <= ?")
        params.append(LIMIT_VAL)
    where_sql = " AND ".join(where)

    with get_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM variation_groups WHERE {where_sql}",
            params,
        ).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT parent_asin, variation_theme, child_count, brand,
                       base_name_en, base_name_ko, ingestion_status, discovered_at,
                       master_asin
                FROM variation_groups
                WHERE {where_sql}
                ORDER BY child_count DESC, discovered_at DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
    items = [dict(r) for r in rows]
    return {"total": total, "channel_limit": LIMIT_VAL, "items": items, "limit": limit, "offset": offset}


@router.get("/stats")
def group_stats(channel: str = Query("coupang"), user: dict = Depends(current_user)):
    LIMIT_VAL = CHANNEL_LIMIT.get(channel, 30)
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM variation_groups").fetchone()["c"]
        over = conn.execute(
            "SELECT COUNT(*) c FROM variation_groups WHERE child_count > ?",
            (LIMIT_VAL,),
        ).fetchone()["c"]
        under = total - over
        avg_count = conn.execute(
            "SELECT AVG(child_count) a FROM variation_groups WHERE child_count > 0"
        ).fetchone()["a"] or 0
        max_count = conn.execute(
            "SELECT MAX(child_count) m FROM variation_groups"
        ).fetchone()["m"] or 0
        rules_count = conn.execute(
            "SELECT COUNT(*) c FROM category_split_rules"
        ).fetchone()["c"]
    return {
        "total_groups": total,
        "over_limit": over,
        "under_limit": under,
        "channel_limit": LIMIT_VAL,
        "avg_child_count": round(avg_count, 1),
        "max_child_count": max_count,
        "category_rules_count": rules_count,
    }


@router.get("/{parent_asin}")
def get_group(parent_asin: str, channel: str = Query("coupang"), user: dict = Depends(current_user)):
    """특정 group 상세 + 자동 분리 미리보기 + 가격 책정."""
    parent_asin = parent_asin.strip().upper()
    g = load_group(parent_asin)
    if not g:
        raise HTTPException(404, "group 없음")

    primary_dim, source = determine_primary_dim(g, channel)
    splits = auto_split(g, channel)
    pricing = calculate_group_pricing(g, channel)
    pricing_by_asin = {p["child_asin"]: p for p in pricing}

    # children 응답 — 핵심 필드만
    children_view = []
    for c in g.get("children") or []:
        ai = c.get("asin")
        p = pricing_by_asin.get(ai, {})
        children_view.append({
            "asin": ai,
            "size_label": c.get("size_label"),
            "color": c.get("color"),
            "flavor_attr": c.get("flavor_attr"),
            "style": c.get("style"),
            "item_weight_g": c.get("item_weight_g"),
            "sales_rank": c.get("sales_rank"),
            "image_url": (c.get("images") or [None])[0],
            "cost_usd": p.get("cost_usd"),
            "sale_krw": p.get("sale_krw"),
            "net_margin_krw": p.get("net_margin_krw"),
            "fee_rate": p.get("fee_rate"),
        })

    return {
        "parent_asin": g.get("parent_asin"),
        "variation_theme": g.get("variation_theme"),
        "variation_dimensions": g.get("variation_dimensions"),
        "child_count": g.get("child_count"),
        "brand": g.get("brand"),
        "base_name_en": g.get("base_name_en"),
        "base_name_ko": g.get("base_name_ko"),
        "category_path": g.get("category_path"),
        "channel_limit": CHANNEL_LIMIT.get(channel, 30),
        "primary_dim": primary_dim,
        "primary_dim_source": source,
        "splits": [
            {
                "name": s.get("name"),
                "size": s.get("size"),
                "split_dim": s.get("split_dim"),
                "split_value": s.get("split_value"),
                "split_value_korean": korean_label(s.get("split_value") or "") or s.get("split_value"),
                "split_source": s.get("split_source"),
                "skipped_count": s.get("skipped_count", 0),
                "option_asins": [o.get("asin") for o in s.get("options") or []],
            }
            for s in splits
        ],
        "children": children_view,
    }


class SaveRuleBody(BaseModel):
    category_path: str
    dim_priority: list[str]


@router.post("/{parent_asin}/save-rule")
def save_rule(parent_asin: str, body: SaveRuleBody, user: dict = Depends(current_user)):
    """사용자 차원 오버라이드 → category_split_rules 누적 학습."""
    if not body.category_path or not body.dim_priority:
        raise HTTPException(400, "category_path / dim_priority 필요")
    save_category_rule(body.category_path, body.dim_priority, source="user")
    return {"ok": True, "category_path": body.category_path, "dim_priority": body.dim_priority}


class ExtendBody(BaseModel):
    channels: list[str] | None = None
    dry_run: bool = True
    confirm: bool = False
    mode: str = "auto"   # 'auto'|'extend'|'register'
    split_indices: list[int] | None = None   # None=all, [0,1]=specific (PoC)


@router.post("/{parent_asin}/backfill-children")
def trigger_backfill_children(parent_asin: str, user: dict = Depends(current_user)):
    """variation_groups 의 children 중 우리 products 에 없는 것 + cost_usd 없는 것을
    SP-API facts + Pricing API 로 일괄 보강.

    백그라운드 잡 (threading.Thread). 진행률은 GET /groups/backfill/{job_id} 폴링.
    """
    import threading, uuid, json as _json
    from backend.purchase.database import get_db
    from backend.purchase.services.group_lister import run_backfill_job

    parent_asin = parent_asin.strip().upper()
    with get_db() as conn:
        vg = conn.execute(
            "SELECT child_asins_json FROM variation_groups WHERE parent_asin=?",
            (parent_asin,),
        ).fetchone()
        if not vg:
            raise HTTPException(404, "variation_groups 없음")
        try:
            child_asins = _json.loads(vg["child_asins_json"] or "[]")
        except Exception:
            child_asins = []
        if not child_asins:
            raise HTTPException(404, "child_asins 비어있음")
        ph = ",".join("?" * len(child_asins))
        existing_count = conn.execute(
            f"SELECT COUNT(*) c FROM products WHERE asin IN ({ph})", child_asins,
        ).fetchone()["c"]
        cost_missing = conn.execute(
            f"SELECT COUNT(*) c FROM products WHERE asin IN ({ph}) AND (cost_usd IS NULL OR cost_usd <= 0)",
            child_asins,
        ).fetchone()["c"]

    to_insert = len(child_asins) - existing_count
    total = to_insert + cost_missing
    job_id = uuid.uuid4().hex[:12]

    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, processed,
                phase_message, parent_asin, created_at)
               VALUES (?, 'group_backfill', 'pending', ?, 0, ?, ?, datetime('now'))""",
            (job_id, total, f"대기 (insert {to_insert}건 + cost 책정 {cost_missing}건)", parent_asin),
        )
    threading.Thread(
        target=run_backfill_job, args=(job_id, parent_asin), daemon=True
    ).start()
    return {
        "job_id": job_id,
        "parent_asin": parent_asin,
        "to_insert": to_insert,
        "cost_missing": cost_missing,
        "total": total,
    }


class BatchRegisterBody(BaseModel):
    limit: int = 50
    channels: list[str] | None = None
    parent_asins: list[str] | None = None   # 명시적 list — 우선순위


@router.post("/batch-register")
def trigger_batch_register(body: BatchRegisterBody, user: dict = Depends(current_user)):
    """GREEN 후보 그룹 N개 자동 register (background).

    candidates = variation_groups.child_count BETWEEN 2~29 + title_ko + category_path + dimensions 보유.
    이미 listed listing 있는 그룹은 제외.
    """
    import threading, uuid
    from backend.purchase.database import get_db
    from backend.purchase.services.group_lister import register_groups_batch

    n = max(1, min(int(body.limit or 50), 1000))

    if body.parent_asins:
        parent_asins = body.parent_asins[:n]
    else:
      with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT vg.parent_asin
            FROM variation_groups vg
            JOIN products p ON p.parent_asin = vg.parent_asin
            LEFT JOIN (
              SELECT DISTINCT pp.parent_asin
              FROM listings_pa l JOIN products pp ON pp.id = l.product_id
              WHERE l.status = 'listed'
            ) listed ON listed.parent_asin = vg.parent_asin
            WHERE vg.child_count BETWEEN 2 AND 29
              AND p.title_ko IS NOT NULL AND p.title_ko != ''
              AND p.category_path IS NOT NULL AND p.category_path != ''
              AND vg.variation_dimensions IS NOT NULL AND vg.variation_dimensions != '[]'
              AND listed.parent_asin IS NULL
            GROUP BY vg.parent_asin
            ORDER BY vg.child_count DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
      parent_asins = [r["parent_asin"] for r in rows]
    if not parent_asins:
        raise HTTPException(404, "candidates 없음")

    job_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, processed,
                phase_message, created_at)
               VALUES (?, 'group_register_batch', 'pending', ?, 0, ?, datetime('now'))""",
            (job_id, len(parent_asins), f"대기 ({len(parent_asins)} 그룹)"),
        )
    threading.Thread(
        target=register_groups_batch,
        args=(parent_asins, job_id),
        kwargs={"channels": body.channels},
        daemon=True,
    ).start()
    return {"job_id": job_id, "count": len(parent_asins), "first_5": parent_asins[:5]}


@router.post("/master-singleton-register")
def trigger_master_singleton_batch(body: BatchRegisterBody, user: dict = Depends(current_user)):
    """그룹별 master child 1건만 단일 listing 등록 (Tier 0 흐름)."""
    import threading, uuid
    from backend.purchase.database import get_db
    from backend.purchase.services.group_lister import register_master_singletons_batch

    n = max(1, min(int(body.limit or 50), 5000))
    if body.parent_asins:
        parent_asins = body.parent_asins[:n]
    else:
        with get_db() as conn:
            rows = conn.execute(
                f"""
                SELECT vg.parent_asin
                FROM variation_groups vg
                JOIN products p ON p.parent_asin = vg.parent_asin
                LEFT JOIN (
                  SELECT DISTINCT pp.parent_asin
                  FROM listings_pa l JOIN products pp ON pp.id = l.product_id
                  WHERE l.status = 'listed'
                ) listed ON listed.parent_asin = vg.parent_asin
                WHERE p.title_ko IS NOT NULL AND p.title_ko != ''
                  AND listed.parent_asin IS NULL
                GROUP BY vg.parent_asin
                LIMIT ?
                """,
                (n,),
            ).fetchall()
        parent_asins = [r["parent_asin"] for r in rows]
    if not parent_asins:
        raise HTTPException(404, "candidates 없음")

    job_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, processed,
                phase_message, created_at)
               VALUES (?, 'master_singleton_register', 'pending', ?, 0, ?, datetime('now'))""",
            (job_id, len(parent_asins), f"대기 ({len(parent_asins)} 그룹)"),
        )
    threading.Thread(
        target=register_master_singletons_batch,
        args=(parent_asins, job_id),
        kwargs={"channels": body.channels},
        daemon=True,
    ).start()
    return {"job_id": job_id, "count": len(parent_asins), "first_5": parent_asins[:5]}


@router.get("/batch-register/{job_id}")
def get_batch_register(job_id: str, user: dict = Depends(current_user)):
    from backend.purchase.database import get_db
    with get_db() as conn:
        row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "job 없음")
    job = dict(row)
    pct = round(((job.get("processed") or 0) / job["total"] * 100), 1) if job.get("total") else 0
    return {**job, "pct": pct}


@router.get("/{parent_asin}/backfill/active")
def get_active_backfill(parent_asin: str, user: dict = Depends(current_user)):
    """이 그룹의 진행중(running/pending) backfill 잡 1건 반환. 없으면 null."""
    from backend.purchase.database import get_db
    parent_asin = parent_asin.strip().upper()
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, status, processed, total, phase_message
               FROM batch_jobs
               WHERE job_type='group_backfill'
                 AND parent_asin=?
                 AND status IN ('running', 'pending')
               ORDER BY created_at DESC LIMIT 1""",
            (parent_asin,),
        ).fetchone()
    if not row:
        return {"active": None}
    job = dict(row)
    pct = round(((job.get("processed") or 0) / job["total"] * 100), 1) if job.get("total") else 0
    return {"active": {**job, "pct": pct}}


@router.get("/backfill/{job_id}")
def backfill_status(job_id: str, user: dict = Depends(current_user)):
    from backend.purchase.database import get_db
    with get_db() as conn:
        row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "job 없음")
    job = dict(row)
    pct = round(((job.get("processed") or 0) / job["total"] * 100), 1) if job.get("total") else 0
    return {**job, "pct": pct}


@router.post("/listings/{listing_id}/backfill-option-ids")
def backfill_option_ids(listing_id: int, user: dict = Depends(current_user)):
    """listing_options.channel_option_id NULL 인 row 들 채널 GET 으로 backfill."""
    from backend.purchase.services.group_lister import backfill_listing_options_channel_ids
    res = backfill_listing_options_channel_ids(listing_id)
    if "error" in res:
        raise HTTPException(404, res["error"])
    return res


@router.post("/{parent_asin}/extend")
def extend_group(parent_asin: str, body: ExtendBody, user: dict = Depends(current_user)):
    """옵션 통합 등록 — mode 자동 분기.

    mode='auto':
      master listing 있음 → extend (기존 master 에 옵션만 추가, 다른 listing archive)
      master listing 없음 → register (신규 multi-option 등록)
    dry_run=True: 페이로드 빌드 + 영향 분석만
    dry_run=False + confirm=True: 실제 채널 호출 + DB 변경
    """
    from backend.purchase.services.group_lister import extend_master_with_group
    parent_asin = parent_asin.strip().upper()
    if not body.dry_run and not body.confirm:
        raise HTTPException(400, "실등록은 confirm=true 필요")
    if body.mode not in ("auto", "extend", "register"):
        raise HTTPException(400, "mode 는 auto/extend/register")
    res = extend_master_with_group(
        parent_asin,
        channels=body.channels,
        dry_run=body.dry_run,
        mode=body.mode,
        split_indices=body.split_indices,
    )
    if "error" in res:
        raise HTTPException(404, res["error"])
    return res


@router.get("/{parent_asin}/payload")
def get_group_payload(
    parent_asin: str,
    channel: str = Query("smartstore"),
    split_index: int = Query(0, ge=0),
    user: dict = Depends(current_user),
):
    """dry-run 페이로드 dump — 사용자 검수용.

    한 split 의 multi-option 페이로드를 JSON 으로 반환.
    실제 채널 등록은 안 함. UI 미리보기 / 셀러센터 수동 등록 시 활용.
    """
    from backend.purchase.services.group_lister import (
        build_smartstore_payload, build_coupang_payload,
    )

    parent_asin = parent_asin.strip().upper()
    g = load_group(parent_asin)
    if not g:
        raise HTTPException(404, "group 없음")
    if channel not in ("smartstore", "coupang"):
        raise HTTPException(400, "channel 은 smartstore/coupang")

    splits = auto_split(g, channel)
    if split_index >= len(splits):
        raise HTTPException(400, f"split_index 범위 초과 (총 {len(splits)})")
    split = splits[split_index]

    pricing = calculate_group_pricing(g, channel)
    by_asin = {p["child_asin"]: p for p in pricing}
    opt_asins = [o.get("asin") for o in split.get("options") or []]
    sp_pricing = [by_asin[a] for a in opt_asins if a in by_asin]

    if not sp_pricing:
        raise HTTPException(422, "이 split 에 가격 책정 가능한 children 없음")

    if channel == "smartstore":
        payload = build_smartstore_payload(g, split, sp_pricing)
    else:
        payload = build_coupang_payload(g, split, sp_pricing)

    if not payload:
        raise HTTPException(422, "페이로드 빌드 실패 (이미지 없음 또는 master 미보유 가능)")

    return {
        "channel": channel,
        "split_index": split_index,
        "split_name": split.get("name"),
        "options_count": len(sp_pricing),
        "payload": payload,
    }
