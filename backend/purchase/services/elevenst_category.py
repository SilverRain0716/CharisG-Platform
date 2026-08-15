"""elevenst_category.py — 아마존 분류 → 11번가 카테고리 매핑 에이전트.

왜 상품이 아니라 '타입' 단위로 매핑하나
--------------------------------------
SP-API 가 주는 `sp_product_type`(VEHICLE_MAT 같은 통제 어휘)과
`sp_browse_classification`(Floor Mats 같은 말단 노드명)은 한국어 상품명보다
훨씬 정확한 신호다. 그리고 종류가 유한하다 — 상품 254,728건이 타입 1,232종으로 접힌다.

  · 상품마다 AI 를 부르면 25만 회, 타입 단위면 1,200여 회
  · 같은 타입이 서로 다른 카테고리로 흩어지는 일이 사라진다
  · 나중에 채널이 늘어도 '타입 → 그 채널 카테고리' 표만 하나 더 만들면 된다

★쿠팡 카테고리를 번역하지 않는다. 두 채널은 체계가 완전히 다르다
  (계량컵: 11번가 '주방용품>홈베이킹용품', 쿠팡 '주방용품>주방잡화>계량용품').
  공통 입력은 카테고리가 아니라 아마존 분류 + 상품명이다.

정확도 정책
-----------
  score >= 70  캐시에 저장하고 자동 사용
  score <  70  category_review_queue 로 보내 사람이 본다
후보에 정답이 없으면 AI 가 스스로 낮은 점수를 준다 — 그 신호를 버리지 않는다.
"""
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

REVIEW_THRESHOLD = 70

DDL = """
CREATE TABLE IF NOT EXISTS product_type_category_map (
    type_key       TEXT PRIMARY KEY,     -- 'PT:VEHICLE_MAT' | 'BC:Floor Mats'
    source_kind    TEXT NOT NULL,        -- product_type | browse
    source_value   TEXT NOT NULL,
    sample_title   TEXT,                 -- 판단 근거로 쓴 상품명(추적용)
    elevenst_no    TEXT,                 -- dispCtgrNo (문자열 — 앞자리 0 보존)
    elevenst_path  TEXT,
    score          INTEGER,
    reason         TEXT,
    decided_by     TEXT DEFAULT 'ai',    -- ai | human
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
IDX = "CREATE INDEX IF NOT EXISTS idx_ptcm_no ON product_type_category_map(elevenst_no)"


def _db():
    from backend.purchase.database import get_db
    return get_db()


# sp_product_type 은 굵기가 제각각이다. VEHICLE_MAT 은 카매트 하나를 뜻하지만
# AUTO_PART 안에는 browse 가 184종(스타터·브레이크패드·발전기…) 들어 있다.
# 굵은 타입을 한 카테고리에 밀어 넣으면 '자동차 스타터모터 → 오토바이 엔진용품'
# 같은 답이 나온다(실측). 그래서 굵으면 browse 까지 키에 붙여 한 칸 더 쪼갠다.
COARSE_BROWSE_KINDS = 30
_card_cache: dict = {}


def browse_kinds(product_type: str) -> int:
    """이 product_type 안에 browse 가 몇 종이나 있나. 굵기 판정용."""
    pt = (product_type or "").strip()
    if not pt:
        return 0
    if pt in _card_cache:
        return _card_cache[pt]
    try:
        with _db() as conn:
            n = conn.execute(
                "SELECT COUNT(DISTINCT sp_browse_classification) c FROM products "
                "WHERE sp_product_type=? AND sp_browse_classification IS NOT NULL "
                "  AND sp_browse_classification != ''", (pt,)).fetchone()["c"]
    except Exception:
        n = 0
    _card_cache[pt] = n
    return n


def type_key(product_type: str = "", browse: str = "") -> Optional[str]:
    """캐시 키 — 3단계.

        PT:VEHICLE_MAT              타입이 좁으면 여기서 끝
        PT:AUTO_PART|BC:Starters    굵으면 browse 까지 (여전히 캐시됨)
        BC:Frames                   product_type 이 없을 때

    ★둘 다 비면 None — 타입 매핑을 포기하라는 뜻이다. 빈 문자열을 키로 쓰면
      서로 다른 상품이 한 칸에 뭉쳐 전부 같은 카테고리로 간다.
    """
    pt = (product_type or "").strip()
    bc = (browse or "").strip()
    if pt:
        if bc and browse_kinds(pt) >= COARSE_BROWSE_KINDS:
            return f"PT:{pt}|BC:{bc}"
        return f"PT:{pt}"
    if bc:
        return f"BC:{bc}"
    return None


def lookup(key: str) -> Optional[dict]:
    if not key:
        return None
    with _db() as conn:
        conn.execute(DDL)
        r = conn.execute(
            "SELECT elevenst_no, elevenst_path, score, reason, decided_by "
            "FROM product_type_category_map WHERE type_key=? AND elevenst_no IS NOT NULL",
            (key,)).fetchone()
    if not r:
        return None
    return {"code": r["elevenst_no"], "path": r["elevenst_path"], "score": r["score"],
            "reason": r["reason"], "source": f"cache/{r['decided_by']}"}


def save(key: str, kind: str, value: str, sample: str, res: dict, by: str = "ai") -> None:
    with _db() as conn:
        conn.execute(DDL)
        conn.execute(IDX)
        conn.execute(
            """INSERT INTO product_type_category_map
                 (type_key, source_kind, source_value, sample_title,
                  elevenst_no, elevenst_path, score, reason, decided_by, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(type_key) DO UPDATE SET
                 elevenst_no=excluded.elevenst_no, elevenst_path=excluded.elevenst_path,
                 score=excluded.score, reason=excluded.reason,
                 decided_by=excluded.decided_by, updated_at=datetime('now')""",
            (key, kind, value, (sample or "")[:120], res.get("code"), res.get("path"),
             res.get("score"), (res.get("reason") or "")[:200], by))


# ── 후보 검색 ────────────────────────────────────────────────
def search_candidates(terms: list[str], limit_per_term: int = 10,
                      account: str = "new") -> list[dict]:
    """말단 카테고리 후보 검색.

    ★global_dlv 로 거르지 않는다. 해외배송 불가 표시(gblDlvYn=N) 카테고리에도
      실제로는 등록된다(2026-08-11 실측: 골프용품 1020704 등록 성공).
      그 조건을 걸면 정답이 후보에서 통째로 빠진다 — 골프·자동차부품 오매칭의 원인이었다.

    대신 계정 능력치로 거른다: 일반 셀러는 '해외직구 > …' 계열에 등록할 수 없다.
    """
    from backend.purchase.services.elevenst_account_caps import candidate_filter_sql
    extra_sql, extra_args = candidate_filter_sql(account)
    out, seen = [], set()
    with _db() as conn:
        for t in terms:
            t = (t or "").strip()
            if len(t) < 2:
                continue
            for r in conn.execute(
                """SELECT disp_no, name, full_path FROM elevenst_categories
                   WHERE (name LIKE ? OR full_path LIKE ?)
                     AND is_leaf=1""" + extra_sql + " LIMIT ?",
                (f"%{t}%", f"%{t}%", *extra_args, limit_per_term),
            ):
                if r["disp_no"] in seen:
                    continue
                seen.add(r["disp_no"])
                out.append({"code": r["disp_no"], "name": r["name"],
                            "path": r["full_path"] or r["name"]})
    return out


def _gemini(prompt: str, json_mode: bool = False, timeout: int = 25):
    from backend.purchase.services.category_mapper import _gemini_rotate
    return _gemini_rotate(prompt, json_mode=json_mode, timeout=timeout)


def korean_terms(product_type: str, browse: str, title_ko: str) -> list[str]:
    """영문 아마존 분류 → 한국어 검색어.

    11번가 카테고리 경로는 한국어라 'Floor Mats' 로는 LIKE 가 안 걸린다.
    분류를 한국어 카테고리 용어로 바꾸는 이 한 단계가 후보 품질을 좌우한다.
    """
    from backend.purchase.services.category_mapper import _korean_tokens
    terms = _korean_tokens(title_ko or "")[:4]
    sig = " / ".join(x for x in [product_type, browse] if x)
    if not sig:
        return terms
    prompt = (
        f"아마존 상품 분류를 한국 오픈마켓 카테고리 검색어로 바꿔라.\n"
        f"분류: {sig}\n"
        f"상품명(한국어): {title_ko[:60]}\n\n"
        f"규칙: 한국 쇼핑몰 카테고리에 실제로 쓰이는 단어 3~6개. "
        f"일반명사 위주(브랜드·모델명 금지). JSON 배열로만 응답.\n"
        f'예: ["자동차매트","카매트","차량용품"]'
    )
    try:
        txt = _gemini(prompt, json_mode=True, timeout=15)
        got = json.loads(txt) if txt else []
        if isinstance(got, dict):
            got = got.get("keywords") or got.get("terms") or []
        got = [str(x).strip() for x in got if str(x).strip()]
        return (got + terms)[:8]
    except Exception as e:
        logger.warning("[11st-cat] 검색어 생성 실패: %s", e)
        return terms


# ── 매핑 본체 ────────────────────────────────────────────────
def map_type(product_type: str, browse: str, title_ko: str,
             sample_titles: list = None, use_cache: bool = True,
             account: str = "new") -> dict:
    """아마존 타입 → 11번가 카테고리. 캐시 우선.

    반환: {code, path, score, reason, source, needs_review}
    """
    key = type_key(product_type, browse)
    if key:
        # ★글로벌 셀러와 일반 셀러는 쓸 수 있는 카테고리가 다르다. 캐시를 공유하면
        #   일반 셀러가 '해외직구' 카테고리를 물려받아 등록에서 거부된다.
        from backend.purchase.services.elevenst_account_caps import is_global_seller
        key = f"{key}|G" if is_global_seller(account) else f"{key}|N"
    if not key:
        return {"code": None, "path": "", "score": 0, "needs_review": True,
                "source": "no-signal", "reason": "sp_product_type·browse 둘 다 없음"}

    if use_cache:
        hit = lookup(key)
        if hit:
            hit["needs_review"] = False
            return hit

    terms = korean_terms(product_type, browse, title_ko)
    cands = search_candidates(terms, account=account)

    # ★검색어가 구체적일수록 LIKE 가 안 걸린다. '자동차 휠 스페이서' 는 어떤 11번가
    #   말단 경로에도 그대로 들어있지 않다(실측: AUTO_ACCESSORY·AUTO_PART 후보 0건).
    #   구를 낱말로 쪼개 넓힌다 — '스페이서', '자동차' 각각으로는 걸린다.
    if not cands:
        words = {w for t in terms for w in re.split(r"[\s/·,]+", t) if len(w) >= 2}
        cands = search_candidates(sorted(words), limit_per_term=8, account=account)
        if cands:
            logger.info("[11st-cat] %s 낱말 분해로 후보 %d건 확보", key, len(cands))

    if not cands:
        return {"code": None, "path": "", "score": 0, "needs_review": True,
                "source": "no-candidate", "reason": f"후보 0건 (검색어: {terms[:4]})"}

    cand_text = "\n".join(f"- {c['code']}: {c['path']}" for c in cands[:40])
    samples = ""
    if sample_titles:
        lines = "\n".join(f"  - {t}" for t in sample_titles[:3] if t)
        if lines:
            samples = f"\n같은 타입의 다른 상품:\n{lines}"

    prompt = (
        f"아마존 상품 타입에 맞는 11번가 카테고리를 고르세요.\n\n"
        f"아마존 product_type: {product_type or '(없음)'}\n"
        f"아마존 분류(browse): {browse or '(없음)'}\n"
        f"상품명(한국어): {title_ko[:80]}{samples}\n\n"
        f"후보:\n{cand_text}\n\n"
        f"규칙:\n"
        f"1. 반드시 위 후보 중 하나. 코드는 문자열 그대로.\n"
        f"2. 이 선택은 같은 타입의 모든 상품에 적용된다 — 특정 상품에만 맞는 "
        f"카테고리가 아니라 타입 전체를 담을 수 있는 곳을 고를 것.\n"
        f"3. 후보에 적절한 것이 없으면 score 를 40 이하로 줄 것(억지로 고르지 말 것).\n"
        f"4. JSON 만: {{\"code\":\"1008712\",\"score\":85,\"reason\":\"한 줄\"}}"
    )
    try:
        txt = _gemini(prompt, json_mode=True)
        parsed = json.loads(txt) if txt else {}
    except Exception as e:
        logger.warning("[11st-cat] AI 실패 %s: %s", key, e)
        return {"code": None, "path": "", "score": 0, "needs_review": True,
                "source": "ai-error", "reason": str(e)[:80]}

    code = str(parsed.get("code") or "").strip()
    score = int(parsed.get("score") or 0)
    matched = next((c for c in cands if c["code"] == code), None)
    if not matched:
        logger.warning("[11st-cat] 후보 외 응답 %s: %s", key, code)
        return {"code": None, "path": "", "score": 0, "needs_review": True,
                "source": "off-list", "reason": f"후보에 없는 코드 {code}"}

    res = {"code": matched["code"], "path": matched["path"], "score": score,
           "reason": parsed.get("reason", ""), "source": "ai",
           "needs_review": score < REVIEW_THRESHOLD}
    if not res["needs_review"]:
        kind = ("product_type+browse" if key.startswith("PT:") and "|BC:" in key
                else "product_type" if product_type else "browse")
        save(key, kind, product_type or browse, title_ko, res)
    return res


def map_product(row) -> dict:
    """products 행 하나 → 11번가 카테고리."""
    return map_type(
        (row["sp_product_type"] if "sp_product_type" in row.keys() else "") or "",
        (row["sp_browse_classification"] if "sp_browse_classification" in row.keys() else "") or "",
        (row["title_ko"] if "title_ko" in row.keys() else "") or "",
    )
