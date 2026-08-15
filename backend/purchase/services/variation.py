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


# 2026-08-03: 쿠팡 공식문서(Product Creation) 확인 — "최대 200개 옵션 등록 가능".
#   기존 30 은 근거 없는 값으로, 자식 31개부터 불필요하게 분할돼 한 그룹이 여러 상품으로
#   흩어졌다(실측 B08KC69CSJ 자식 75개 → 그룹 5개, 옵션 합계 16개).
CHANNEL_LIMIT = {"coupang": 30, "smartstore": 100}

# 옵션 axis 값에 들어가면 안 되는 noise 패턴 (Pack-of N, 카테고리 단어, +결합, 60자 초과 등).
# 신규 등록 차단용. 매칭되면 그 옵션/split drop.
_NOISE_AXIS_RE = re.compile(
    r"""
    \bPack\s*of\s*\d+\b |     # "Pack of 2"
    \b\d+\s*Pack\b |          # "2 Pack" / "3 Pack"
    \bPcs?\b |                # "Pcs" / "Pc"
    \bUnits?\b |              # "Unit"
    \bServings?\b |           # "Servings"
    \bsnorkeling\b |          # 일반 카테고리 단어 (axis 값에 들어오면 노이즈)
    \bdiving\b |
    \bswimming\b |
    \bdrinkware\b |
    \bscavenger\b |
    \+                        # "Black+Green" 같은 결합 기호
    """,
    re.IGNORECASE | re.VERBOSE,
)
_NOISE_AXIS_MAXLEN = 60


def is_noisy_axis_value(value: str | None) -> bool:
    """옵션 axis 값에 noise 패턴이 있으면 True.

    rule 1: '+' 결합 / 'Pack of N' / 'N Pack' / 'Pcs' / 카테고리 단어 포함.
    rule 2: 60자 초과.
    rule 3: 빈/None.
    """
    if not value:
        return True
    v = str(value).strip()
    if not v:
        return True
    if len(v) > _NOISE_AXIS_MAXLEN:
        return True
    if _NOISE_AXIS_RE.search(v):
        return True
    return False
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
                f"""SELECT asin, sp_api_facts_json, cost_usd, weight_g, parent_asin, title_ko
                    FROM products
                    WHERE asin IN ({placeholders})""",
                child_asins,
            ).fetchall()
            facts_by_asin: dict[str, dict] = {}
            _best_l: dict = {}
            for r in rows:
                # ★중복 asin → 가장 완전한 행 선택 (cost>0 → title_ko → facts). 2026-06-30
                _sc = (1 if (r["cost_usd"] and r["cost_usd"] > 0) else 0,
                       1 if (r["title_ko"] or "").strip() else 0,
                       1 if r["sp_api_facts_json"] else 0)
                if r["asin"] in _best_l and _sc <= _best_l[r["asin"]]:
                    continue
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
                _best_l[r["asin"]] = _sc
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

    # 2️⃣ split 개수 최소 + cv 분산도 + 변형 수 균형
    # — primary cardinality 가 클수록 split 폭발(쿠팡 sellerProduct 다수 등록 → 카탈로그 오염).
    #   그룹화의 본질은 "한 listing 에 최대 옵션" 이므로 n_splits 가 작을수록 우선.
    candidates: list[tuple[str, int, float, int]] = []
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
        n_splits = len(groups)
        if max_group_size > LIMIT:
            # 한도 초과 → secondary 로 sub-split 필요 — 우선순위 후순위 (n_splits 페널티)
            n_splits = n_splits + max_group_size  # 강한 페널티
        candidates.append((dim, n_splits, cv, max_group_size))
    if candidates:
        # n_splits 적을수록 우선 (1 listing 에 옵션 다수), 동률이면 cv 큰 것, 그 다음 그룹 크기 작은 것
        candidates.sort(key=lambda x: (x[1], -x[2], x[3]))
        primary = candidates[0][0]
        if candidates[0][2] > 0.10 and group.get("category_path"):
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


# 색상 영문 → 한글 (자주 쓰는 것 위주; 미등록은 원문 유지)
_KOREAN_COLOR_MAP = {
    "black": "블랙", "white": "화이트", "red": "레드", "blue": "블루", "green": "그린",
    "pink": "핑크", "yellow": "옐로우", "purple": "퍼플", "gray": "그레이", "grey": "그레이",
    "orange": "오렌지", "brown": "브라운", "beige": "베이지", "navy": "네이비", "gold": "골드",
    "silver": "실버", "mint": "민트", "ivory": "아이보리", "khaki": "카키", "cyan": "시안",
    "violet": "바이올렛", "rose": "로즈", "teal": "틸", "clear": "투명", "transparent": "투명",
    "multicolor": "멀티컬러", "multi": "멀티", "charcoal": "차콜",
    # 확장 (2026-05-30) — 실제 옵션 라벨에 등장한 영문 색
    "lime": "라임", "sky": "스카이", "tomato": "토마토", "indigo": "인디고",
    "cerulean": "세룰리안", "slate": "슬레이트", "maroon": "마룬", "crimson": "크림슨",
    "magenta": "마젠타", "olive": "올리브", "lavender": "라벤더", "coral": "코랄",
    "salmon": "샐먼", "aqua": "아쿠아", "turquoise": "터쿼이즈", "tan": "탄",
    "forest": "포레스트", "lemon": "레몬", "mustard": "머스타드", "peach": "피치",
    "plum": "플럼", "wine": "와인", "sage": "세이지", "cream": "크림",
    "fuchsia": "푸시아", "neon": "네온", "scarlet": "스칼렛", "ruby": "루비",
    "emerald": "에메랄드", "jade": "제이드", "amber": "앰버", "azure": "애저",
    # 확장 v2.2 (2026-05-31) — audit 잔여 특수 색상
    "lilac": "라일락", "amethyst": "아메시스트", "sunset": "선셋", "ocean": "오션",
    "citrus": "시트러스", "seaglass": "씨글라스", "seafoam": "씨폼",
    "stealth": "스텔스", "urchin": "어친", "sapphire": "사파이어", "skyblue": "스카이블루",
    "lightblue": "라이트블루", "navyblue": "네이비블루", "pinkred": "핑크레드",
    "bblack": "블랙",  # 자명한 BBlack 오타
    "casual": "캐주얼",  # 스타일 단어
    # 보조 modifier (다크/라이트/딥/브라이트/핫)
    "dark": "다크", "light": "라이트", "deep": "딥", "bright": "브라이트", "hot": "핫",
    "pale": "페일", "rich": "리치",
}

# 재질 영문 → 한글 (material 옵션축용)
_KOREAN_MATERIAL_MAP = {
    "vinyl": "비닐", "plastic": "플라스틱", "steel": "스틸", "stainless": "스테인리스",
    "wood": "나무", "wooden": "원목", "glass": "유리", "aluminum": "알루미늄",
    "bamboo": "대나무", "silicone": "실리콘", "ceramic": "세라믹", "leather": "가죽",
    "cotton": "면", "polyester": "폴리에스터", "nylon": "나일론", "metal": "메탈",
    "rubber": "고무", "linen": "리넨", "wool": "울", "acrylic": "아크릴",
    "tempered": "강화",
}

# 사이즈는 영문 유지 — 사용자 피드백 (Small/Medium/Large/XL 가 국제 표준, 한글 변환 시 어색).
# 토큰 번역 통합 사전 (색상 + 재질)
_KOREAN_TOKEN_MAP = {**_KOREAN_COLOR_MAP, **_KOREAN_MATERIAL_MAP}


def _is_color_like(value: Optional[str]) -> bool:
    """값의 모든 알파 토큰이 색상사전에 있으면 True (size 차원의 색상 오염 판별용)."""
    if not value:
        return False
    toks = [t for t in re.split(r"[\s\-/]+", value.strip()) if t]
    return bool(toks) and all(t.lower() in _KOREAN_COLOR_MAP for t in toks)


def _strip_common_affix(values: list[str]) -> list[str]:
    """형제 옵션값들이 공유하는 공통 접두/접미 토큰 제거.

    예: ["Green-accessory","Pink-accessory"] → ["Green","Pink"] (공통 'accessory' 제거).
    구분에 무의미한 공통 토큰을 떼어 깔끔한 옵션 라벨을 만든다.
    """
    if len(values) < 2:
        return list(values)
    toks = [[t for t in re.split(r"[\s\-/]+", v.strip()) if t] for v in values]
    while all(len(t) >= 2 for t in toks) and len({t[-1].lower() for t in toks}) == 1:
        for t in toks:
            t.pop()
    while all(len(t) >= 2 for t in toks) and len({t[0].lower() for t in toks}) == 1:
        for t in toks:
            t.pop(0)
    return [" ".join(t) for t in toks]


def korean_label(label) -> str:
    """size_label / color / flavor / number_of_items 등 값 → 한글 표기.

    단위 변환(Pack 등) + 색상·재질 단어 한글화. 미등록 토큰/자유 텍스트는 원문 유지.
    숫자(int/float) 등 비-str 입력은 str() 강제(number_of_items=5 같은 케이스 대응).
    """
    if label is None or label == "":
        return ""
    s = str(label).strip()
    for pat, repl in _KOREAN_UNIT_MAP:
        s = pat.sub(repl, s)
    # 색상·재질 단어 토큰 번역 (구분자 유지)
    s = "".join(_KOREAN_TOKEN_MAP.get(tok.lower(), tok) for tok in re.split(r"([\s/\-]+)", s))
    return s


# ── 옵션 axis 값 정규화 (strip 노이즈 + 한글 통일 + fuzzy 오타 정정) ──
_NOISE_STRIP_RE = re.compile(
    r"""
    \(?\bPack\s*of\s*\d+\)? |   # "Pack of 2" / "(Pack of 1)"
    \b\d+\s*Pack\b |             # "2 Pack" / "3 Pack"
    \b\d+-Pack\b |               # "1-Pack" / "2-Pack" (hyphen)
    \bPACK[-:\s]?\d+ |           # "PACK-2"
    \bPcs?\b |
    \bUnits?\b |
    \bServings?\b |
    \bsnorkeling\b | \bdiving\b | \bswimming\b |
    \bdrinkware\b | \bscavenger\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# fuzzy 오타 정정 후보 (영문 색상 표준)
_COLOR_FUZZY_DICT = tuple(_KOREAN_COLOR_MAP.keys()) + tuple(_KOREAN_MATERIAL_MAP.keys())

# 사이즈/연령/측정 단위 — 영문 그대로 통과 (feedback: 사이즈는 영문 유지).
# clean_axis_value 의 영문 토큰 처리에서 한글 변환 / fuzzy 정정 우회.
_SIZE_PASSTHROUGH = {
    # 사이즈 letter
    "s", "m", "l", "xs", "xl", "xxl", "xxxl", "ml",
    "small", "medium", "large", "xsmall", "xlarge",
    "lrg", "med", "sml", "lge",   # 약어
    # 표준
    "one", "size", "free", "regular", "standard",
    # 연령/대상
    "adult", "adults", "kid", "kids", "youth", "child", "children", "infant",
    "baby", "junior", "mens", "womens", "men", "women", "unisex",
    # 사이즈 system
    "us", "eu", "uk", "jp", "kr",
    # 핏/길이
    "fit", "long", "short", "slim", "wide",
    # 기타 modifier
    "new", "old", "original",
    # ★방향/착용 (장갑 등) — fuzzy 색상정정 오작동 방지: right→bright(브라이트) 오역 차단.
    "right", "left", "hand", "worn", "both", "pair", "side",
}


def _fuzzy_color_correct(token_lower: str) -> Optional[str]:
    """편집거리 ~1 영문 색상 오타 정정. 단일 후보 일치만 정정, 모호하면 None."""
    if not token_lower or len(token_lower) < 3:
        return None
    if token_lower in _KOREAN_TOKEN_MAP:
        return token_lower  # 정확 일치
    import difflib
    matches = difflib.get_close_matches(token_lower, _COLOR_FUZZY_DICT, n=2, cutoff=0.85)
    if len(matches) == 1:
        return matches[0]
    return None


def _is_hangul(s: str) -> bool:
    return any("가" <= ch <= "힣" for ch in s)


def clean_axis_value(value, max_len: int = 24) -> Optional[str]:
    """옵션 axis 값 정규화.

    1) 노이즈 strip ('2 Pack', 'snorkeling' 등)
    2) 토큰 분해 (구분자 보존)
    3) 각 영문 토큰:
       - 한글 통일 (_KOREAN_TOKEN_MAP)
       - 오타는 fuzzy 정정 후 한글
       - 정정 불가 + 한글 아님 → 토큰 drop
    4) 빈/매우 짧음 → None 반환 (caller 가 옵션 drop)
    5) max_len 초과 자름
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) > 80:  # 너무 긴 raw 는 그 자체로 노이즈
        return None

    # 1) 노이즈 텍스트 strip
    s = _NOISE_STRIP_RE.sub(" ", s)
    # 단위 변환 (Pack/Packs → 팩) — korean_label 의 _KOREAN_UNIT_MAP 활용
    for pat, repl in _KOREAN_UNIT_MAP:
        s = pat.sub(repl, s)

    # 2) 토큰 분해 — 구분자 (\s, /, -, +) 보존
    parts = re.split(r"([\s/\-+]+)", s)
    out_tokens = []
    for p in parts:
        if not p:
            continue
        if re.fullmatch(r"[\s/\-+]+", p):
            out_tokens.append(p)
            continue
        # 알파벳 토큰: 한글 매핑 → 한글 / 사이즈 사전 → 원문 / fuzzy 정정 → 한글 / 그 외 → 원문 유지
        if re.fullmatch(r"[A-Za-z]+", p):
            low = p.lower()
            if low in _KOREAN_TOKEN_MAP:
                out_tokens.append(_KOREAN_TOKEN_MAP[low])
            elif low in _SIZE_PASSTHROUGH:
                # 사이즈/연령/측정 단위 — 영문 원문 보존
                out_tokens.append(p)
            else:
                corrected = _fuzzy_color_correct(low)
                if corrected:
                    out_tokens.append(_KOREAN_TOKEN_MAP[corrected])
                else:
                    # 알 수 없는 영문 토큰 (style/pattern/material 등) → 원문 유지
                    # 정보 손실 방지 — 옵션 중복 발생 줄임.
                    out_tokens.append(p)
        else:
            # 한글, 숫자, 혼합 (예: '12.7온스', 'XL(Adult')  → 그대로
            out_tokens.append(p)

    # 3) 재결합 + separator 정리 — `/`, `+` 는 의미 보존, 공백·hyphen 정리 + dangling 제거
    result = "".join(out_tokens).strip()
    result = re.sub(r"\s+", " ", result)              # 다중 공백 → 단일
    result = re.sub(r"\s*/\s*", " / ", result)        # slash 양쪽 단일 공백
    result = re.sub(r"\s*\+\s*", "+", result)         # plus 공백 제거
    result = re.sub(r"\s*-\s*", "-", result)          # hyphen 공백 제거
    result = re.sub(r"-{2,}", "-", result)            # 연속 하이픈 정리 ("1--블랙" → "1-블랙")
    result = re.sub(r"^[\-]+|[\-]+$", "", result)     # 양끝 하이픈 제거
    result = re.sub(r"^[\s/\-+]+|[\s/\-+]+$", "", result)  # 양끝 구분자 제거

    if not result or len(result) < 2:
        return None
    if len(result) > max_len:
        # 공백 절약 retry — slash 양옆 공백 제거하면 길이 줄어듦.
        compact = re.sub(r"\s*/\s*", "/", result).strip()
        if len(compact) <= max_len:
            result = compact
        else:
            result = result[:max_len].rstrip()
    return result


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
        # 2026-08-03: 분리축이 없을 때 top N 으로 잘라 나머지를 버렸다(자식 대량 소실).
        #   → 판매순위 순으로 정렬한 뒤 LIMIT 개씩 청크로 나눠 전부 등록한다. 손실 0.
        ordered = _top_n_by_sales_rank(children, len(children)) or list(children)
        out = []
        for ci in range(0, len(ordered), LIMIT):
            part = ordered[ci:ci+LIMIT]
            idx = ci // LIMIT + 1
            out.append({
                "name": (f"{base_name} ({idx})" if len(ordered) > LIMIT else base_name).strip(),
                "options": part,
                "split_dim": None,
                "split_value": None,
                "size": len(part),
                "split_source": "count_chunk",
                "skipped_count": 0,
                "sub_chunk_index": idx if len(ordered) > LIMIT else None,
            })
        return out

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
            # 2026-08-03: secondary 축이 없을 때도 top N 절단으로 나머지를 버렸다.
            #   → LIMIT 개씩 청크로 전부 등록. 손실 0.
            _ord = _top_n_by_sales_rank(sub, len(sub)) or list(sub)
            for ci in range(0, len(_ord), LIMIT):
                part = _ord[ci:ci+LIMIT]
                idx = ci // LIMIT + 1
                _sfx = f"{primary_suffix} ({idx})" if len(_ord) > LIMIT else primary_suffix
                listings.append({
                    "name": f"{base_name} {_sfx}".strip(),
                    "options": part,
                    "split_dim": primary,
                    "split_value": dim_value,
                    "size": len(part),
                    "split_source": source + ("+count_chunk" if len(_ord) > LIMIT else ""),
                    "skipped_count": 0,
                    "sub_chunk_index": idx if len(_ord) > LIMIT else None,
                })
    # ★ '_unknown' split drop — primary dim 값 부재 child 는 attrs 매핑 불가로 build_coupang_payload 실패.
    #   레거시 row (parent_asin/sp_api_facts NULL) 가 group_lister.fetch_and_insert_children 보강 전이면 발생.
    #   해당 child 수만 dropped_unknown_count 로 메타에 기록.
    dropped = sum(s.get("size", 0) for s in listings if s.get("split_value") == "_unknown")
    listings = [s for s in listings if s.get("split_value") != "_unknown"]
    if dropped:
        for s in listings:
            s["dropped_unknown_count"] = dropped
    # ★ 노이즈 split_value drop ('Black+Green' / '2 Pack' / 60자초과 등).
    #   primary axis 값 자체가 노이즈면 그 split 의 모든 자식이 같은 노이즈 라벨로 묶임 → 전체 drop.
    noisy_dropped = sum(s.get("size", 0) for s in listings if is_noisy_axis_value(s.get("split_value")))
    listings = [s for s in listings if not is_noisy_axis_value(s.get("split_value"))]
    if noisy_dropped:
        for s in listings:
            s["dropped_noisy_count"] = s.get("dropped_noisy_count", 0) + noisy_dropped
        logger.info(f"[auto_split] {group.get('parent_asin')} noisy split drop — children {noisy_dropped}건")
    # ★2026-08-04 과분할 수습: 옵션 1개짜리 split 은 멀티옵션 리스팅이 될 수 없어
    #   전부 자식 단일폴백으로 흩어졌다(자식96→split92 같은 케이스). primary 축 값이
    #   자식마다 고유하면 그건 분리축이 아니라 옵션축이다. 잔여 단일들을 모아
    #   LIMIT 개씩 묶어 정상 멀티옵션 리스팅으로 복원한다.
    _singles = [s for s in listings if (s.get("size") or 0) == 1]
    if len(_singles) >= 2:
        _left = [o for s in _singles for o in (s.get("options") or [])]
        listings = [s for s in listings if (s.get("size") or 0) != 1]
        for _ci in range(0, len(_left), LIMIT):
            _part = _left[_ci:_ci + LIMIT]
            _idx = _ci // LIMIT + 1
            listings.append({
                "name": (f"{base_name} ({_idx})" if len(_left) > LIMIT else base_name).strip(),
                "options": _part,
                "split_dim": primary,
                "split_value": None,
                "size": len(_part),
                "split_source": (source or "") + "+merged_singles",
                "skipped_count": 0,
                "sub_chunk_index": _idx if len(_left) > LIMIT else None,
            })
        logger.info(f"[auto_split] {group.get('parent_asin')} 단일split {len(_singles)}개 "
                    f"→ {(len(_left) + LIMIT - 1) // LIMIT}개 멀티옵션으로 병합")

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
        from backend.purchase.services.forwarder_shipping import forwarder_shipping_usd
        from backend.purchase.services.channel_listing_service import _load_default_forwarder_extras
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
            f"SELECT id, asin, cost_usd, title_ko, weight_g FROM products WHERE asin IN ({placeholders})",
            asins,
        ).fetchall()
    # ★중복 asin → 가장 완전한 행 선택 (cost>0 → title_ko → 낮은id). 2026-06-30
    #   기존 dict-comp 은 마지막 행이 덮어써, 불완전 중복행(cost 없음)을 골라 자식이 통째 드롭되던 버그.
    cost_by_asin: dict = {}
    _best_p: dict = {}
    for r in rows:
        _sc = (1 if (r["cost_usd"] and r["cost_usd"] > 0) else 0,
               1 if (r["title_ko"] or "").strip() else 0,
               -(r["id"] or 0))
        if r["asin"] not in _best_p or _sc > _best_p[r["asin"]]:
            _best_p[r["asin"]] = _sc
            cost_by_asin[r["asin"]] = (r["id"], r["cost_usd"], r["weight_g"])

    out = []
    for c in children:
        asin = c.get("asin")
        if not asin:
            continue
        info = cost_by_asin.get(asin)
        if not info:
            continue
        product_id, cost_usd, weight_g = info
        if cost_usd is None or cost_usd <= 0:
            continue
        try:
            _ex = _load_default_forwarder_extras()
            pr = calculate_sale_krw(
                cost_usd=float(cost_usd), amazon_shipping_usd=0.0,
                cj_shipping_usd=forwarder_shipping_usd(weight_g), channel=channel,
                safety_margin_krw=_ex["safety_krw"], cs_cost_krw=_ex["cs_krw"],
                return_reserve_pct=_ex["return_pct"],
            )
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
