"""
variation.py — Phase 3 옵션 그룹 코어 모듈.

함수:
  load_group(parent_asin)              — variation_groups + children facts 합친 group dict
  determine_primary_dim(group, channel) — 4단계 결정 트리 (카테고리 룰 → cv → AI → fallback)
  auto_split(group, channel)            — 옵션 LIMIT 초과 시 사이즈/맛 등 1차 차원으로 분리
  calculate_group_pricing(group, channel) — children 별 sale_krw 산정
  korean_label(size_label)              — "5 Pound (Pack of 1)" → "5파운드"
  match_category_rule / save_category_rule — category_split_rules 학습

분리 LIMIT:
  쿠팡 30 / 네이버 100 (multi-option items 한도)
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from typing import Optional

logger = logging.getLogger(__name__)


CHANNEL_LIMIT = {"coupang": 30, "smartstore": 100}
DEFAULT_FALLBACK_DIMS = ["size", "flavor", "style", "color"]   # AI 실패 시 fallback 순서

# theme 토큰 → 정규화 dim 키 매핑 (SP-API 응답에 다양한 변종)
_DIM_NORMALIZE = {
    "size": "size", "size_name": "size", "sizename": "size",
    "color": "color", "color_name": "color", "colorname": "color",
    "flavor": "flavor", "flavor_name": "flavor", "flavorname": "flavor",
    "style": "style", "style_name": "style", "stylename": "style",
    "pattern": "pattern", "pattern_name": "pattern",
    "scent": "scent", "scent_name": "scent",
    "material": "material", "material_type": "material",
}


def _normalize_dim(token: str) -> Optional[str]:
    if not token:
        return None
    t = token.strip().lower().replace("-", "_")
    return _DIM_NORMALIZE.get(t, t)


def parse_dims_from_theme(theme: str) -> list[str]:
    """SP-API variation_theme ("SIZE/COLOR" / "FLAVOR_NAME/SIZE_NAME") → 정규화된 dim 리스트."""
    if not theme:
        return []
    parts = re.split(r"[/_]+(?=[A-Z])|/", theme)
    # 위 split 은 단순 / 와 _ 분리이지만 SIZE_NAME 같은 케이스 우선 처리:
    # 더 단순: "/" 로 split 후 각 토큰을 _normalize_dim
    raw = theme.split("/")
    out = []
    for r in raw:
        n = _normalize_dim(r)
        if n and n not in out:
            out.append(n)
    return out


def _child_dim_value(child_facts: dict, dim: str) -> Optional[str]:
    """정규화된 dim 키 → child facts 의 해당 차원 값 추출."""
    if not isinstance(child_facts, dict):
        return None
    if dim == "size":
        return child_facts.get("size_label") or child_facts.get("size_attr")
    if dim == "color":
        return child_facts.get("color")
    if dim == "flavor":
        return child_facts.get("flavor_attr")
    if dim == "style":
        return child_facts.get("style")
    if dim == "pattern":
        # raw attributes 안에서 검색 (옵션)
        raw = child_facts.get("style") or child_facts.get("size_label")
        return raw
    return child_facts.get(dim)   # 자유 패스스루


def groupby_dim(children: list[dict], dim: str) -> dict[str, list[dict]]:
    """children list 를 dim_value 별로 묶음 (None 값은 '_unknown' 그룹)."""
    out: dict[str, list[dict]] = {}
    for c in children:
        v = _child_dim_value(c, dim)
        key = v if v else "_unknown"
        out.setdefault(key, []).append(c)
    return out


# ── load_group ───────────────────────────────────────
def load_group(parent_asin: str, fetch_missing: bool = False) -> Optional[dict]:
    """variation_groups + children facts 합친 group dict 반환.

    children facts 는 products.sp_api_facts_json 캐시된 것만 사용 (DB JOIN, SP-API 호출 0회).
    fetch_missing=True 면 캐시 miss 인 children 만 SP-API 호출 — UI 에선 빠른 응답을 위해 False.
    """
    if not parent_asin:
        return None
    parent_asin = parent_asin.strip().upper()

    try:
        from backend.purchase.database import get_db
    except ImportError:
        return None

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM variation_groups WHERE parent_asin=?",
            (parent_asin,),
        ).fetchone()
        if not row:
            return None

        group = dict(row)
        try:
            group["variation_dimensions"] = json.loads(row["variation_dimensions"] or "[]")
        except (json.JSONDecodeError, TypeError):
            group["variation_dimensions"] = []
        try:
            group["child_asins"] = json.loads(row["child_asins_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            group["child_asins"] = []

        child_asins = group["child_asins"]
        children: list[dict] = []
        if child_asins:
            placeholders = ",".join("?" * len(child_asins))
            rows = conn.execute(
                f"""SELECT asin, sp_api_facts_json, cost_usd, weight_g, parent_asin
                    FROM products
                    WHERE asin IN ({placeholders})""",
                child_asins,
            ).fetchall()
            facts_by_asin: dict[str, dict] = {}
            for r in rows:
                facts: dict = {}
                if r["sp_api_facts_json"]:
                    try:
                        facts = json.loads(r["sp_api_facts_json"]) or {}
                    except (json.JSONDecodeError, TypeError):
                        facts = {}
                facts.setdefault("asin", r["asin"])
                if r["cost_usd"] is not None:
                    facts["cost_usd"] = r["cost_usd"]
                if r["weight_g"] is not None:
                    facts.setdefault("item_weight_g", r["weight_g"])
                facts_by_asin[r["asin"]] = facts
            for a in child_asins:
                if a in facts_by_asin:
                    children.append(facts_by_asin[a])
                else:
                    # 캐시 miss — products 테이블에 없는 child ASIN
                    children.append({"asin": a, "_no_facts": True})

    # 옵션: fetch_missing=True 시 캐시 miss 만 SP-API 호출 (느림 — 일반 UI 에선 비활성)
    if fetch_missing:
        try:
            from backend.purchase.services.sp_api_facts import fetch_full_catalog_facts
            for i, c in enumerate(children):
                if c.get("_no_facts"):
                    f = fetch_full_catalog_facts(c["asin"])
                    if f:
                        children[i] = f
        except ImportError:
            pass

    group["children"] = children
    return group


# ── 카테고리 룰 ──────────────────────────────────────
def match_category_rule(category_path: str) -> Optional[list[str]]:
    """category_split_rules 에서 category_path 매칭 (정확 매칭 우선, 부분 매칭 fallback)."""
    if not category_path:
        return None
    try:
        from backend.purchase.database import get_db
    except ImportError:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT preferred_dim_priority FROM category_split_rules WHERE category_path=?",
            (category_path,),
        ).fetchone()
        if row:
            try:
                return json.loads(row["preferred_dim_priority"])
            except (json.JSONDecodeError, TypeError):
                return None
        # 부분 매칭 — category_path 의 prefix 가 룰에 있나
        candidates = conn.execute(
            "SELECT category_path, preferred_dim_priority FROM category_split_rules"
        ).fetchall()
    for c in candidates:
        rp = c["category_path"]
        if rp and category_path.startswith(rp):
            try:
                return json.loads(c["preferred_dim_priority"])
            except (json.JSONDecodeError, TypeError):
                continue
    return None


def save_category_rule(category_path: str, dim_priority: list[str], source: str = "user") -> None:
    """category_split_rules upsert (sample_count 누적)."""
    if not category_path or not dim_priority:
        return
    try:
        from backend.purchase.database import get_db
    except ImportError:
        return
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO category_split_rules
                (category_path, preferred_dim_priority, source, sample_count, updated_at)
               VALUES (?, ?, ?, 1, ?)
               ON CONFLICT(category_path) DO UPDATE SET
                 preferred_dim_priority = excluded.preferred_dim_priority,
                 source = excluded.source,
                 sample_count = category_split_rules.sample_count + 1,
                 updated_at = excluded.updated_at""",
            (category_path, json.dumps(dim_priority, ensure_ascii=False), source, ts),
        )


# ── determine_primary_dim — 4단계 결정 ──────────────
def _coefficient_of_variation(values: list[float]) -> float:
    if not values:
        return 0.0
    arr = [v for v in values if v is not None and v > 0]
    if len(arr) < 2:
        return 0.0
    m = statistics.mean(arr)
    if not m:
        return 0.0
    return statistics.pstdev(arr) / m


def _ai_suggest_primary_dim(group: dict) -> Optional[str]:
    """Gemini 에 group 정보 + sample children 보내고 1 dim 답변 받음. 실패 시 None."""
    try:
        from backend_shared.ai.service import _call_gemini
    except ImportError:
        return None
    dims = group.get("variation_dimensions") or []
    if not dims:
        return None
    sample = []
    for c in (group.get("children") or [])[:5]:
        sample.append({
            "asin": c.get("asin"),
            "size": c.get("size_label"),
            "color": c.get("color"),
            "flavor": c.get("flavor_attr"),
            "cost_usd": (c.get("item_weight_g") or 0),  # 보안: cost 노출 회피용 weight 만 사용
        })
    prompt = (
        "한국 이커머스 상품 등록 시 분리 차원을 결정합니다.\n"
        f"카테고리: {group.get('category_path') or 'unknown'}\n"
        f"브랜드: {group.get('brand') or 'unknown'}\n"
        f"variation theme: {group.get('variation_theme') or 'unknown'}\n"
        f"가능 차원: {dims}\n"
        f"sample children: {json.dumps(sample, ensure_ascii=False)}\n\n"
        "한국 소비자가 검색·구매 결정 시 가장 우선시하는 차원 1개를 답하세요.\n"
        f"반드시 다음 중 하나로만 답하세요: {' / '.join(dims)}\n"
        "다른 텍스트 금지. 한 단어만."
    )
    try:
        resp = _call_gemini(prompt, max_tokens=10)
    except Exception:
        return None
    if not resp:
        return None
    norm = _normalize_dim(resp.strip())
    return norm if norm in dims else None


# 결과 메모리 캐시 (parent_asin, channel) → (dim, source)
# pa-api 프로세스 살아있는 동안 유지. 재시작 시 다시 호출.
_primary_dim_cache: dict[tuple[str, str], tuple[Optional[str], str]] = {}


def determine_primary_dim(group: dict, channel: str = "coupang") -> tuple[Optional[str], str]:
    """4단계 결정 트리 + 메모리 캐시.

    반환: (primary_dim, source) — source 는 'category_rule'|'cv_balance'|'ai'|'fallback'|'cached'.
    primary_dim None 이면 분리 불가.
    """
    cache_key = (group.get("parent_asin") or "", channel)
    if cache_key in _primary_dim_cache:
        cached = _primary_dim_cache[cache_key]
        return cached[0], "cached"

    LIMIT = CHANNEL_LIMIT.get(channel, 30)
    children = group.get("children") or []
    dims = group.get("variation_dimensions") or []

    # 1️⃣ 카테고리 룰 — 사용자 의도 우선. LIMIT 초과는 auto_split 의 sub-split 으로 처리
    rule = match_category_rule(group.get("category_path") or "")
    if rule:
        for dim in rule:
            dim_n = _normalize_dim(dim)
            if dim_n and dim_n in dims:
                groups = groupby_dim(children, dim_n)
                if groups:
                    return dim_n, "category_rule"

    # 2️⃣ 가격 분산도 cv + 변형 수 균형
    candidates: list[tuple[str, float, int]] = []
    for dim in dims:
        groups = groupby_dim(children, dim)
        if not groups:
            continue
        # 그룹별 평균 cost (price 분산도)
        avg_costs = []
        for gv in groups.values():
            costs = [c.get("item_weight_g") for c in gv]   # cost_usd 가 정규화 dict 에 없으면 weight 대용
            # 실제 cost_usd 는 fetch_full_catalog_facts 가 안 가져옴 — products 테이블에서 별도 join 필요
            # 일단 group 안 children 에 cost_usd 가 있으면 사용, 없으면 build_avg_cost helper
            costs = [c.get("cost_usd") for c in gv if c.get("cost_usd")]
            if costs:
                avg_costs.append(statistics.mean(costs))
        cv = _coefficient_of_variation(avg_costs)
        max_group_size = max(len(g) for g in groups.values())
        if max_group_size > LIMIT:
            continue   # 분리 후 한도 초과 — 부적합
        candidates.append((dim, cv, max_group_size))
    if candidates:
        # cv 큰 차원 우선, 동률이면 그룹 크기 작은 것
        candidates.sort(key=lambda x: (-x[1], x[2]))
        primary = candidates[0][0]
        if candidates[0][1] > 0.10 and group.get("category_path"):
            save_category_rule(group["category_path"], [primary], source="auto")
        result = (primary, "cv_balance")
        _primary_dim_cache[cache_key] = result
        return result

    # 3️⃣ 단순 fallback (DEFAULT_FALLBACK_DIMS) — AI 호출보다 먼저 시도해 빠른 응답
    for d in DEFAULT_FALLBACK_DIMS:
        if d in dims:
            result = (d, "fallback")
            _primary_dim_cache[cache_key] = result
            return result

    # 4️⃣ AI fallback (마지막 수단 — Gemini 호출은 비용/시간 큼)
    ai_dim = _ai_suggest_primary_dim(group)
    if ai_dim:
        if group.get("category_path"):
            save_category_rule(group["category_path"], [ai_dim], source="ai")
        result = (ai_dim, "ai")
        _primary_dim_cache[cache_key] = result
        return result

    result = (None, "none")
    _primary_dim_cache[cache_key] = result
    return result


# ── 한글 라벨 변환 (단순 dictionary) ──────────────────
_KOREAN_UNIT_MAP = [
    (re.compile(r"(\d+(?:\.\d+)?)\s*Pound\s*(?:\(Pack of (\d+)\))?", re.IGNORECASE),
     lambda m: f"{m.group(1)}파운드" + (f" {m.group(2)}팩" if m.group(2) else "")),
    (re.compile(r"(\d+(?:\.\d+)?)\s*Ounces?(?:\s*Pack\s*of\s*(\d+))?", re.IGNORECASE),
     lambda m: f"{m.group(1)}온스" + (f" {m.group(2)}팩" if m.group(2) else "")),
    (re.compile(r"(\d+(?:\.\d+)?)\s*Count\s*(?:\(Pack of (\d+)\))?", re.IGNORECASE),
     lambda m: f"{m.group(1)}개" + (f" {m.group(2)}팩" if m.group(2) else "")),
    (re.compile(r"(\d+(?:\.\d+)?)\s*L\b", re.IGNORECASE),
     lambda m: f"{m.group(1)}L"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*ml\b", re.IGNORECASE),
     lambda m: f"{m.group(1)}ml"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*kg\b", re.IGNORECASE),
     lambda m: f"{m.group(1)}kg"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*g\b", re.IGNORECASE),
     lambda m: f"{m.group(1)}g"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*inch(?:es)?\b", re.IGNORECASE),
     lambda m: f"{m.group(1)}인치"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*cm\b", re.IGNORECASE),
     lambda m: f"{m.group(1)}cm"),
]


def korean_label(label: Optional[str]) -> str:
    """size_label / color / flavor 영문값 → 한글 표기.

    단순 단위 변환 + Pack 표기. 색상명 / 맛 같은 자유 텍스트는 그대로 유지 (추후
    translation_cache 활용 가능).
    """
    if not label:
        return ""
    s = label.strip()
    for pat, repl in _KOREAN_UNIT_MAP:
        s = pat.sub(repl, s)
    return s


# ── auto_split — 옵션 한도 초과 시 분리 ──────────────
def auto_split(group: dict, channel: str = "coupang") -> list[dict]:
    """그룹을 채널 한도에 맞춰 listing 분리.

    반환: [
      {"name", "options": [child_facts...], "split_dim": ..., "split_value": ...,
       "size": N},
      ...
    ]
    """
    LIMIT = CHANNEL_LIMIT.get(channel, 30)
    children = group.get("children") or []
    base_name = group.get("base_name_ko") or group.get("base_name_en") or ""

    if not children:
        return []
    if len(children) <= LIMIT:
        return [{
            "name": base_name,
            "options": children,
            "split_dim": None,
            "split_value": None,
            "size": len(children),
        }]

    primary, source = determine_primary_dim(group, channel)
    if not primary:
        # 분리 불가능 — top N 자르기
        cut = _top_n_by_sales_rank(children, LIMIT)
        return [{
            "name": base_name,
            "options": cut,
            "split_dim": None,
            "split_value": None,
            "size": len(cut),
            "split_source": "limited_top_n",
            "skipped_count": len(children) - len(cut),
        }]

    listings: list[dict] = []
    all_dims = group.get("variation_dimensions") or []
    secondary = next((d for d in all_dims if d != primary), None)

    for dim_value, sub in groupby_dim(children, primary).items():
        primary_suffix = korean_label(dim_value) or dim_value

        if len(sub) <= LIMIT:
            listings.append({
                "name": f"{base_name} {primary_suffix}".strip(),
                "options": sub,
                "split_dim": primary,
                "split_value": dim_value,
                "size": len(sub),
                "split_source": source,
                "skipped_count": 0,
            })
        elif secondary:
            # 한도 초과 → secondary 차원으로 sub-split (같은 secondary 값은 한 chunk 유지)
            sub_groups = groupby_dim(sub, secondary)
            chunks = _chunk_by_secondary(sub_groups, LIMIT)
            for chunk_idx, chunk_options in enumerate(chunks, 1):
                range_label = _value_range_label(chunk_options, secondary)
                chunk_suffix = f"{primary_suffix} {range_label}" if range_label else f"{primary_suffix} ({chunk_idx})"
                listings.append({
                    "name": f"{base_name} {chunk_suffix}".strip(),
                    "options": chunk_options,
                    "split_dim": primary,
                    "split_value": dim_value,
                    "size": len(chunk_options),
                    "split_source": source + ("+sub_split" if len(chunks) > 1 else ""),
                    "skipped_count": 0,
                    "sub_chunk_index": chunk_idx if len(chunks) > 1 else None,
                    "sub_chunk_label": range_label or None,
                })
        else:
            # secondary 차원 없음 → top N 자르기 (기존 동작)
            sub_use = _top_n_by_sales_rank(sub, LIMIT)
            listings.append({
                "name": f"{base_name} {primary_suffix}".strip(),
                "options": sub_use,
                "split_dim": primary,
                "split_value": dim_value,
                "size": len(sub_use),
                "split_source": source,
                "skipped_count": len(sub) - len(sub_use),
            })
    return listings


def _chunk_by_secondary(sub_groups: dict[str, list[dict]], limit: int) -> list[list[dict]]:
    """secondary 차원 그룹들을 limit 이하 단위로 chunk. 같은 secondary 값은 한 chunk 유지."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for sec_value in sorted(sub_groups.keys(), key=lambda x: (x or '_')):
        items = sub_groups[sec_value]
        if len(items) > limit:
            if current:
                chunks.append(current); current = []
            chunks.append(_top_n_by_sales_rank(items, limit))
            continue
        if len(current) + len(items) > limit:
            chunks.append(current); current = []
        current.extend(items)
    if current:
        chunks.append(current)
    return chunks


def _value_range_label(options: list[dict], dim: str) -> str:
    """chunk 내 차원 값들의 첫 글자 범위 (예: 'A-M'). 한 글자만 있으면 그 글자."""
    values = sorted({(_child_dim_value(c, dim) or '').strip() for c in options})
    values = [v for v in values if v]
    if not values:
        return ""
    first = values[0][0].upper()
    last = values[-1][0].upper()
    return first if first == last else f"{first}-{last}"


def _top_n_by_sales_rank(children: list[dict], n: int) -> list[dict]:
    """sales_rank 가 작은 것(=잘 팔림) 우선 N 개. rank None 은 후순위."""
    def key(c):
        r = c.get("sales_rank")
        return (r if r is not None else 10**9, c.get("asin") or "")
    return sorted(children, key=key)[:n]


# ── calculate_group_pricing — children 별 가격 ──────
def calculate_group_pricing(group: dict, channel: str) -> list[dict]:
    """children 별 calculate_sale_krw 호출.

    반환: [{"child_asin", "child_product_id", "sale_krw", "cost_krw", ...}, ...]
    """
    try:
        from backend.purchase.services.pricing_service_pa import calculate_sale_krw
        from backend.purchase.database import get_db
    except ImportError:
        return []

    children = group.get("children") or []
    # cost_usd 는 facts 에 없을 수 있어 products 테이블에서 join 로드
    asins = [c.get("asin") for c in children if c.get("asin")]
    if not asins:
        return []
    placeholders = ",".join("?" * len(asins))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, asin, cost_usd FROM products WHERE asin IN ({placeholders})",
            asins,
        ).fetchall()
    cost_by_asin = {r["asin"]: (r["id"], r["cost_usd"]) for r in rows}

    out = []
    for c in children:
        asin = c.get("asin")
        if not asin:
            continue
        info = cost_by_asin.get(asin)
        if not info:
            continue
        product_id, cost_usd = info
        if cost_usd is None or cost_usd <= 0:
            continue
        try:
            pr = calculate_sale_krw(cost_usd=float(cost_usd), channel=channel)
        except Exception as e:
            logger.warning(f"[group-pricing] {asin} 가격 산정 실패: {e}")
            continue
        out.append({
            "child_asin": asin,
            "child_product_id": product_id,
            "size_label": c.get("size_label"),
            "color": c.get("color"),
            "flavor": c.get("flavor_attr"),
            "option_label": _build_option_label(c),
            "cost_usd": float(cost_usd),
            **pr,   # sale_krw / cost_krw / fee_rate / net_margin_krw / target_margin_rate
        })
    return out


def _build_option_label(child_facts: dict) -> str:
    """children 의 차원 값들을 ' / ' 결합한 옵션 라벨."""
    parts = []
    for dim_key, label_key in [("size_label", "size_label"), ("color", "color"),
                                ("flavor_attr", "flavor_attr"), ("style", "style")]:
        v = child_facts.get(label_key)
        if v:
            parts.append(korean_label(v) or v)
    return " / ".join(parts) if parts else "기본"
