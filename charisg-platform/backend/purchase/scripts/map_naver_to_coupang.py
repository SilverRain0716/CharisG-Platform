"""
map_naver_to_coupang.py — 네이버 카테고리 → 쿠팡 카테고리 자동 매핑.

3단계 fallback:
1) Stage 1 (exact): naver.name == coupang.name (leaf+ACTIVE)
2) Stage 2 (path): whole_name 마지막 N토큰이 쿠팡 path에 포함
3) Stage 3 (AI): 잔여 → Gemini로 후보 short-list에서 best pick

매핑 결과는 naver_coupang_category_map 테이블에 저장.
listings_pa.coupang_category_code도 함께 갱신.

실행:
    cd /home/ubuntu/CharisG-Platform/charisg-platform
    set -a && source .env && set +a
    python3 -m backend.purchase.scripts.map_naver_to_coupang [--only-pa] [--no-ai] [--dry-run]
"""
import argparse
import json
import logging
import re
import sys
import time
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

from backend.purchase.database import get_db, init_db


# ── Stage 1: 정확 이름 매칭 ──────────────────────────────────

def stage1_exact(naver_id: str, naver_name: str) -> Optional[tuple[int, str]]:
    """coupang_categories.name == naver_name인 leaf+ACTIVE 코드.
    다중 매칭 시 path가 가장 짧은 것 (구체성보다 일반성 우선)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT code, path, length(path) AS plen
               FROM coupang_categories
               WHERE name=? AND is_leaf=1 AND status='ACTIVE'
               ORDER BY plen ASC LIMIT 1""",
            (naver_name,),
        ).fetchall()
    if rows:
        return rows[0]["code"], f"exact:{rows[0]['path']}"
    return None


# ── Stage 2: path 토큰 매칭 ──────────────────────────────────

def _normalize_token(s: str) -> str:
    """공백/특수문자 제거 + 소문자."""
    return re.sub(r'[\s/·\-_]+', '', s).lower()


def _split_leaf_tokens(leaf: str) -> list[str]:
    """'바스/비치타월' → ['바스', '비치타월']. 슬래시·하이픈으로 분리."""
    parts = re.split(r'[/·\-_]+', leaf)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2]


def stage2_path(naver_id: str, naver_name: str, whole_name: str) -> Optional[tuple[int, str]]:
    """whole_name의 마지막 2~3 토큰이 쿠팡 path에 순서대로 포함된 leaf 코드.
    leaf의 슬래시 토큰을 분리해서 각 부분에 대해서도 시도."""
    if not whole_name:
        return None
    tokens = [t.strip() for t in re.split(r'>', whole_name) if t.strip()]
    if not tokens:
        return None
    leaf = tokens[-1]
    parent = tokens[-2] if len(tokens) >= 2 else None
    grand = tokens[-3] if len(tokens) >= 3 else None

    leaf_parts = _split_leaf_tokens(leaf) or [leaf]

    with get_db() as conn:
        candidates: list[tuple[int, str]] = []
        # 시도 1: leaf 부분 토큰 + parent path 포함
        for lp in leaf_parts:
            if parent:
                rows = conn.execute(
                    """SELECT code, path FROM coupang_categories
                       WHERE name LIKE ? AND is_leaf=1 AND status='ACTIVE' AND path LIKE ?
                       ORDER BY length(path) ASC LIMIT 5""",
                    (f"%{lp}%", f"%{parent}%"),
                ).fetchall()
                candidates.extend([(r["code"], r["path"]) for r in rows])
            if candidates:
                break
        # 시도 2: leaf 부분 토큰만 (parent 무관)
        if not candidates:
            for lp in leaf_parts:
                rows = conn.execute(
                    """SELECT code, path FROM coupang_categories
                       WHERE name LIKE ? AND is_leaf=1 AND status='ACTIVE'
                       ORDER BY length(path) ASC LIMIT 5""",
                    (f"%{lp}%",),
                ).fetchall()
                candidates.extend([(r["code"], r["path"]) for r in rows])
                if candidates:
                    break
        # 시도 3: parent를 leaf로 대체 (한 단계 위)
        if not candidates and parent:
            rows = conn.execute(
                """SELECT code, path FROM coupang_categories
                   WHERE name LIKE ? AND is_leaf=1 AND status='ACTIVE'
                   ORDER BY length(path) ASC LIMIT 5""",
                (f"%{parent}%",),
            ).fetchall()
            candidates = [(r["code"], r["path"]) for r in rows]

    if not candidates:
        return None

    def score(path: str) -> int:
        s = 0
        if parent and parent in path:
            s += 2
        if grand and grand in path:
            s += 1
        for lp in leaf_parts:
            if lp in path:
                s += 1
        return s

    candidates.sort(key=lambda c: -score(c[1]))
    code, path = candidates[0]
    return code, f"path:{path}"


# ── Stage 3: AI 매핑 ─────────────────────────────────────────

def _find_candidates(whole_name: str, leaf: str, leaf_parts: list[str], parent: Optional[str], top_k: int) -> list[dict]:
    """광범위한 후보 풀 수집 — leaf 토큰들 + parent 토큰을 OR로."""
    seen = set()
    candidates: list[dict] = []
    with get_db() as conn:
        # 1순위: leaf 부분 토큰 매칭
        for lp in leaf_parts:
            rows = conn.execute(
                """SELECT code, name, path FROM coupang_categories
                   WHERE is_leaf=1 AND status='ACTIVE'
                     AND (name LIKE ? OR path LIKE ?)
                   ORDER BY length(path) ASC LIMIT ?""",
                (f"%{lp}%", f"%{lp}%", top_k),
            ).fetchall()
            for r in rows:
                if r["code"] not in seen:
                    seen.add(r["code"])
                    candidates.append({"code": r["code"], "name": r["name"], "path": r["path"]})
            if len(candidates) >= top_k:
                break
        # 2순위: parent 매칭으로 보충
        if len(candidates) < top_k and parent:
            rows = conn.execute(
                """SELECT code, name, path FROM coupang_categories
                   WHERE is_leaf=1 AND status='ACTIVE' AND path LIKE ?
                   ORDER BY length(path) ASC LIMIT ?""",
                (f"%{parent}%", top_k),
            ).fetchall()
            for r in rows:
                if r["code"] not in seen:
                    seen.add(r["code"])
                    candidates.append({"code": r["code"], "name": r["name"], "path": r["path"]})
        # 3순위: 대분류 토큰
        if len(candidates) < 5:
            tokens = [t.strip() for t in re.split(r'>', whole_name or "") if t.strip()]
            if tokens:
                top = tokens[0]
                rows = conn.execute(
                    """SELECT code, name, path FROM coupang_categories
                       WHERE is_leaf=1 AND status='ACTIVE' AND path LIKE ?
                       ORDER BY length(path) ASC LIMIT ?""",
                    (f"%{top}%", top_k),
                ).fetchall()
                for r in rows:
                    if r["code"] not in seen:
                        seen.add(r["code"])
                        candidates.append({"code": r["code"], "name": r["name"], "path": r["path"]})
    return candidates[:top_k]


COUPANG_ROOT_DOMAINS = [
    "패션의류잡화", "뷰티", "출산/유아동", "식품", "주방용품",
    "생활용품", "가구/홈데코", "가전/디지털", "스포츠/레져",
    "자동차용품", "도서", "문구/오피스", "음반/DVD",
    "완구/취미", "반려/애완용품", "기프트카드",
]


def _ai_pick_root_domain(whole_name: str) -> Optional[str]:
    """AI에게 쿠팡 16개 대분류 중 1개 선택 위임."""
    from backend_shared.ai.service import _call_gemini

    domains_text = "\n".join(f"- {d}" for d in COUPANG_ROOT_DOMAINS)
    prompt = f"""네이버 카테고리를 쿠팡의 대분류 1개로 분류하세요.

네이버 카테고리: {whole_name}

쿠팡 대분류 (반드시 이 중에서 선택):
{domains_text}

가장 적합한 대분류 1개를 선택하세요. JSON으로만 응답: {{"root": "대분류명"}}"""

    try:
        result = _call_gemini(prompt, max_tokens=100)
    except Exception as e:
        logger.error(f"  대분류 AI 예외: {e}")
        return None
    if not result:
        return None
    m = re.search(r'\{[^}]+\}', result)
    if not m:
        return None
    try:
        parsed = json.loads(m.group())
        root = str(parsed.get("root", "")).strip()
        if root in COUPANG_ROOT_DOMAINS:
            return root
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


def _find_candidates_in_root(root_name: str, top_k: int) -> list[dict]:
    """쿠팡 대분류 path 하위의 leaf 카테고리 일부 (top_k)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT code, name, path FROM coupang_categories
               WHERE is_leaf=1 AND status='ACTIVE' AND path LIKE ?
               ORDER BY RANDOM() LIMIT ?""",
            (f"{root_name}%", top_k),
        ).fetchall()
    return [{"code": r["code"], "name": r["name"], "path": r["path"]} for r in rows]


def stage3_ai(naver_id: str, naver_name: str, whole_name: str, top_k: int = 50) -> Optional[tuple[int, str]]:
    """Gemini로 후보 short-list에서 best pick (강화 버전).

    1) 후보 검색 (leaf+parent 토큰 매칭)
    2) 후보 0개면 → AI에게 대분류 위임 → 그 대분류 leaves에서 다시 AI pick
    """
    from backend_shared.ai.service import _call_gemini

    if not whole_name:
        whole_name = naver_name
    tokens = [t.strip() for t in re.split(r'>', whole_name) if t.strip()]
    leaf = tokens[-1] if tokens else naver_name
    parent = tokens[-2] if len(tokens) >= 2 else None
    leaf_parts = _split_leaf_tokens(leaf) or [leaf]

    candidates = _find_candidates(whole_name, leaf, leaf_parts, parent, top_k)

    method_prefix = "ai"
    if not candidates:
        # 2단계 AI — 대분류 결정 → 그 대분류 leaves에서 best pick
        logger.info(f"  후보 0개 → 2단계 AI 진입: {whole_name}")
        root = _ai_pick_root_domain(whole_name)
        if not root:
            logger.warning(f"  대분류 AI 실패: {whole_name}")
            return None
        candidates = _find_candidates_in_root(root, top_k)
        method_prefix = "ai-root"
        if not candidates:
            logger.warning(f"  대분류 {root} 하위 leaf 0개")
            return None

    candidates_text = "\n".join(f"  [{c['code']}] {c['path']}" for c in candidates)
    prompt = f"""네이버 쇼핑 카테고리를 쿠팡 카테고리로 매핑하세요.

네이버 카테고리: {whole_name}

쿠팡 후보 (반드시 이 코드 중에서만 선택):
{candidates_text}

가장 의미가 일치하는 쿠팡 카테고리 1개의 code를 골라주세요.
의미가 비슷한 게 없으면 가장 가까운 것을 선택하세요. 절대 "없음"으로 답하지 마세요.
JSON으로만 응답: {{"code": <number>}}"""

    try:
        result = _call_gemini(prompt, max_tokens=200)
    except Exception as e:
        logger.error(f"  AI 호출 예외: {e}")
        result = None

    chosen: Optional[int] = None
    if result:
        try:
            m = re.search(r'\{[^}]+\}', result)
            if m:
                parsed = json.loads(m.group())
                chosen = int(parsed.get("code", 0))
        except (json.JSONDecodeError, ValueError, TypeError):
            chosen = None

    candidate_codes = {c["code"] for c in candidates}
    if chosen and chosen in candidate_codes:
        chosen_path = next(c["path"] for c in candidates if c["code"] == chosen)
        return chosen, f"{method_prefix}:{chosen_path}"

    # AI가 후보 외/실패 → top-1 후보로 강제 매핑 (최후 보장)
    fallback = candidates[0]
    logger.info(f"  AI 폴백 → 후보 top-1: [{fallback['code']}] {fallback['path']}")
    return fallback["code"], f"{method_prefix}-fallback:{fallback['path']}"


# ── 매핑 메인 ────────────────────────────────────────────────

def get_target_naver_ids(only_pa: bool) -> list[str]:
    with get_db() as conn:
        if only_pa:
            rows = conn.execute(
                """SELECT DISTINCT category_mapped FROM listings_pa
                   WHERE channel='coupang' AND category_mapped IS NOT NULL"""
            ).fetchall()
            return [str(r["category_mapped"]) for r in rows]
        else:
            rows = conn.execute("SELECT id FROM naver_categories").fetchall()
            return [str(r["id"]) for r in rows]


def get_already_mapped() -> set[str]:
    with get_db() as conn:
        rows = conn.execute("SELECT naver_id FROM naver_coupang_category_map").fetchall()
        return {r["naver_id"] for r in rows}


def save_mapping(naver_id: str, coupang_code: int, method: str, note: str, dry_run: bool):
    if dry_run:
        return
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO naver_coupang_category_map
               (naver_id, coupang_code, method, confidence, note, mapped_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (naver_id, coupang_code, method, 1.0 if method != 'ai' else 0.85, note),
        )


def update_listings_pa(dry_run: bool) -> int:
    """listings_pa.coupang_category_code를 매핑 결과로 일괄 갱신."""
    if dry_run:
        return 0
    with get_db() as conn:
        cur = conn.execute(
            """UPDATE listings_pa
               SET coupang_category_code = (
                 SELECT coupang_code FROM naver_coupang_category_map
                 WHERE naver_id = listings_pa.category_mapped
               ),
               coupang_category_resolved_at = datetime('now')
               WHERE channel='coupang' AND category_mapped IS NOT NULL"""
        )
        return cur.rowcount


def main():
    parser = argparse.ArgumentParser(description="네이버→쿠팡 카테고리 자동 매핑 (3-stage fallback)")
    parser.add_argument("--only-pa", action="store_true",
                        help="listings_pa에 사용 중인 카테고리만 (default: 전체 4,993개)")
    parser.add_argument("--no-ai", action="store_true", help="Stage 3 AI 비활성화 (Stage 1+2만)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ai-sleep", type=float, default=1.0, help="AI 호출 간 대기(초)")
    parser.add_argument("--limit", type=int, default=0, help="처리 개수 제한 (테스트용)")
    parser.add_argument("--redo-fallbacks", action="store_true",
                        help="기존 fallback/ai-fallback 매핑을 삭제 후 재시도")
    args = parser.parse_args()

    init_db()

    if args.redo_fallbacks:
        with get_db() as conn:
            n = conn.execute(
                """DELETE FROM naver_coupang_category_map
                   WHERE note LIKE 'fallback:%' OR note LIKE 'ai-fallback:%'"""
            ).rowcount
        logger.info(f"기존 fallback 매핑 {n}건 삭제 — 재시도 진행")

    target_ids = get_target_naver_ids(only_pa=args.only_pa)
    already = get_already_mapped()
    pending = [nid for nid in target_ids if nid not in already]
    if args.limit:
        pending = pending[: args.limit]

    logger.info(f"대상 네이버 카테고리: {len(target_ids)} / 이미 매핑: {len(already)} / 처리 대상: {len(pending)}")

    counters = {"exact": 0, "path": 0, "ai": 0, "fail": 0}
    t0 = time.time()

    for i, naver_id in enumerate(pending, 1):
        with get_db() as conn:
            row = conn.execute(
                "SELECT name, whole_name FROM naver_categories WHERE id=?",
                (naver_id,),
            ).fetchone()
        if not row:
            logger.warning(f"  [{i}/{len(pending)}] naver_id={naver_id} 없음")
            counters["fail"] += 1
            continue
        naver_name = row["name"]
        whole_name = row["whole_name"]

        # Stage 1
        result = stage1_exact(naver_id, naver_name)
        if result:
            code, note = result
            save_mapping(naver_id, code, "exact", note, args.dry_run)
            counters["exact"] += 1
            if i % 50 == 0:
                logger.info(f"  [{i}/{len(pending)}] 진행: {counters}")
            continue

        # Stage 2
        result = stage2_path(naver_id, naver_name, whole_name)
        if result:
            code, note = result
            save_mapping(naver_id, code, "path", note, args.dry_run)
            counters["path"] += 1
            if i % 50 == 0:
                logger.info(f"  [{i}/{len(pending)}] 진행: {counters}")
            continue

        # Stage 3 AI
        if args.no_ai:
            counters["fail"] += 1
            logger.info(f"  [{i}/{len(pending)}] {naver_id} ({whole_name}) → 매핑 실패 (AI 비활성)")
            continue

        result = stage3_ai(naver_id, naver_name, whole_name)
        if result:
            code, note = result
            save_mapping(naver_id, code, "ai", note, args.dry_run)
            counters["ai"] += 1
            logger.info(f"  [{i}/{len(pending)}] AI: {whole_name} → {note}")
        else:
            counters["fail"] += 1
            logger.warning(f"  [{i}/{len(pending)}] {naver_id} ({whole_name}) → 매핑 실패")
        time.sleep(args.ai_sleep)

    elapsed = time.time() - t0
    logger.info("─" * 60)
    logger.info(f"매핑 완료 ({elapsed:.1f}s) — {counters}")
    logger.info(f"성공률: {(counters['exact'] + counters['path'] + counters['ai']) / max(len(pending), 1) * 100:.1f}%")

    if not args.dry_run:
        n = update_listings_pa(dry_run=False)
        logger.info(f"listings_pa.coupang_category_code 갱신: {n}건")


if __name__ == "__main__":
    main()
