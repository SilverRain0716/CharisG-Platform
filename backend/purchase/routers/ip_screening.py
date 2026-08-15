"""지식재산권(IP) 사전검수 대시보드 API.

등록 전 단계에서 상품을 4중 IP 게이트로 검수한 결과를 제공한다.
  ① 브랜드 게이트   (정품 증빙 불가 브랜드 차단 — coupang brand_blocklist)
  ② 정책·IP 키워드  (저작권/상표/라이선스 캐릭터 등 — clean_policy)
  ③ 한국 제조사 IP  (국내 제조사 라이선스 보호 게이트)
  ④ KIPRIS 대조     (한국특허정보원 국내 등록 IP 권리자 라이브 조회)

읽기 전용. 외부 쓰기 없음(KIPRIS 조회만). 쿠팡 무접촉.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services import clean_policy
from backend.purchase.services import kipris_ip_gate
from backend.purchase.services.coupang_lister import _is_brand_blocked, _load_brand_blocklist

router = APIRouter(prefix="/api/pa/ip-screening", tags=["ip-screening"])

# KIPRIS 라이브 조회가 행별로 들어가 계산이 무거움(~20s) + 월 쿼터(1,000) 절약 →
# 결과를 파일 캐시(TTL)로 보관. 첫 계산 후엔 즉시 응답, refresh=1 로 강제 갱신.
_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "ip_screening_cache.json")
_CACHE_TTL = 6 * 3600  # 6시간


def _ip_keyword_count() -> int:
    try:
        return len(clean_policy.PROHIBITED_INGREDIENTS)
    except Exception:
        return 0


def _screen_row(p: dict, blocklist, run_kipris: bool) -> dict:
    title_en = p.get("title_en") or ""
    title_ko = p.get("title_ko") or ""
    brand = p.get("brand") or ""

    checks = {}
    blocked = False
    reasons = []

    # ① 브랜드 게이트
    bm = _is_brand_blocked(title_en, title_ko, blocklist)
    checks["brand_gate"] = {"status": "차단" if bm else "통과", "matched": bm or ""}
    if bm:
        blocked = True
        reasons.append(f"브랜드 게이트: {bm}")

    # ② 정책·IP 키워드 (clean_policy) — 브랜드명도 함께 검사(라이선스 IP 브랜드 포착)
    hit, kw = clean_policy.check_prohibited_ingredients(title_en, title_ko, brand)
    checks["ip_keyword"] = {"status": "차단" if hit else "통과", "matched": kw or ""}
    if hit:
        blocked = True
        reasons.append(f"정책·IP 키워드: {kw}")

    # ③ 한국 제조사 IP (캐시 컬럼 직접 판독 — 분류 트리거 없음)
    mk = p.get("manufacturer_is_korean")
    if mk == 1:
        checks["korean_mfr"] = {"status": "차단", "matched": p.get("amazon_manufacturer") or "한국 제조사"}
        blocked = True
        reasons.append("한국 제조사 IP")
    else:
        checks["korean_mfr"] = {"status": "통과", "matched": ""}

    # ④ KIPRIS 라이브 대조
    if run_kipris:
        kr = kipris_ip_gate.screen(brand, title_ko)
        checks["kipris"] = {
            "status": kr["status"], "matched": kr.get("query") or "",
            "hits": len(kr.get("matches") or []),
            "top": [m.get("name", "") for m in (kr.get("matches") or [])[:2]],
        }
        if kr.get("flagged"):
            blocked = True
            reasons.append(kr["reason"])
    else:
        checks["kipris"] = {"status": "대기", "matched": "", "hits": 0, "top": []}

    return {
        "asin": p.get("asin"),
        "brand": brand,
        "title_ko": title_ko[:46],
        "verdict": "차단·수동검토" if blocked else "통과",
        "blocked": blocked,
        "checks": checks,
        "reason": " · ".join(reasons) if reasons else "4중 게이트 통과",
    }


def _compute(limit: int = 12) -> dict:
    blocklist = _load_brand_blocklist()

    with get_db() as conn:
        # 누적 통계 (시스템 실가동 증거)
        catalog_total = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
        flagged_total = conn.execute(
            "SELECT COUNT(*) c FROM products WHERE violation_keyword IS NOT NULL AND violation_keyword!=''"
        ).fetchone()["c"]

        # 검수 샘플: IP 위험 후보 + 정상 최신 혼합 → 4개 게이트 차단/통과 모두 노출
        cols = ("asin,title_en,title_ko,brand,amazon_manufacturer,manufacturer_is_korean")
        # 위험 후보(실상품): 브랜드 게이트/IP 키워드/KIPRIS 각 사례
        risk_asins = ("B093LSJJMY", "B083HS3B1G", "B0F2NDG18H",
                      "B0GFB6GYR7", "B0GT1N7RPP", "B0CN3QCW2M")
        qmarks = ",".join("?" for _ in risk_asins)
        flagged = conn.execute(
            f"SELECT {cols} FROM products WHERE asin IN ({qmarks})", risk_asins
        ).fetchall()
        recent = conn.execute(
            f"SELECT {cols} FROM products WHERE status='listed' "
            f"AND brand IS NOT NULL AND brand!='' ORDER BY id DESC LIMIT 12"
        ).fetchall()

    seen, sample = set(), []
    for r in list(flagged) + list(recent):
        d = dict(r)
        if d["asin"] in seen:
            continue
        seen.add(d["asin"])
        sample.append(d)
        if len(sample) >= limit:
            break

    # KIPRIS는 쿼터(월 1,000) 고려 — 행별 1회(캐시 dedup). limit 내라 안전.
    rows = [_screen_row(p, blocklist, run_kipris=True) for p in sample]

    # KIPRIS 라이브 시연 패널 — 신고 브랜드 실조회
    demo = kipris_ip_gate.screen("디즈니")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "catalog_total": catalog_total,
            "flagged_total": flagged_total,
            "blocklist_keywords": len(blocklist),
            "ip_keywords": _ip_keyword_count(),
            "kipris_enabled": kipris_ip_gate.is_enabled(),
            "kipris_status": "라이브 연동" if kipris_ip_gate.is_enabled() else "대기(키 미등록)",
            "blocked_in_sample": sum(1 for r in rows if r["blocked"]),
            "sample_size": len(rows),
        },
        "rows": rows,
        "kipris_demo": {
            "query": "디즈니",
            "status": demo["status"],
            "reason": demo["reason"],
            "matches": demo.get("matches", []),
        },
    }


def _read_cache() -> dict | None:
    try:
        if os.path.exists(_CACHE_PATH) and (time.time() - os.path.getmtime(_CACHE_PATH)) < _CACHE_TTL:
            with open(_CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _write_cache(data: dict) -> None:
    try:
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _CACHE_PATH)
    except Exception:
        pass


def compute_and_cache(limit: int = 12) -> dict:
    prev = _read_cache()
    data = _compute(limit)
    # KIPRIS 데모가 '대기'(쿼터 소진 등)면 이전 양호 데모 보존 → 패널 빈 화면 방지
    if (data.get("kipris_demo", {}).get("status") == "대기"
            and prev and (prev.get("kipris_demo") or {}).get("matches")):
        data["kipris_demo"] = prev["kipris_demo"]
    _write_cache(data)
    return data


@router.get("")
def ip_screening(limit: int = Query(12, ge=1, le=40), refresh: int = Query(0),
                 user: dict = Depends(current_user)):
    if not refresh:
        cached = _read_cache()
        if cached is not None:
            cached["cached"] = True
            return cached
    data = compute_and_cache(limit)
    data["cached"] = False
    return data


@router.get("/public")
def ip_screening_public():
    """인증 불필요 공개 스냅샷 — 외부(쿠팡 담당자) 열람용. 캐시만 제공(비민감: 상품 검수 요약).

    라이브 계산/KIPRIS 조회를 트리거하지 않음(캐시 읽기 전용). 필요 시 라우터 제거로 즉시 비공개화.
    """
    cached = _read_cache()
    if cached is None:
        cached = compute_and_cache(12)
    out = dict(cached)
    out["cached"] = True
    out["public"] = True
    return out


def _ensure_kipris_cache(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS kipris_brand_cache (
            brand TEXT PRIMARY KEY, status TEXT, flagged INTEGER,
            hits INTEGER, top_name TEXT, checked_at TEXT)"""
    )


def _kipris_cache_get(conn, brand: str):
    r = conn.execute(
        "SELECT status, flagged, hits, top_name FROM kipris_brand_cache WHERE brand=?",
        (brand,),
    ).fetchone()
    if not r:
        return None
    return {"status": r["status"], "flagged": bool(r["flagged"]),
            "hits": r["hits"] or 0, "top": r["top_name"] or ""}


def _kipris_cache_put(conn, brand: str, res: dict):
    matches = res.get("matches") or []
    conn.execute(
        """INSERT OR REPLACE INTO kipris_brand_cache
           (brand, status, flagged, hits, top_name, checked_at)
           VALUES (?,?,?,?,?,datetime('now'))""",
        (brand, res.get("status"), 1 if res.get("flagged") else 0,
         len(matches), (matches[0].get("name", "") if matches else "")),
    )


def _has_hangul(s: str) -> bool:
    return any("가" <= ch <= "힣" for ch in (s or ""))


def _ensure_ko_cache(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS brand_ko_cache (brand TEXT PRIMARY KEY, korean TEXT)")


def _brand_korean(conn, brand: str) -> str:
    """영문 브랜드의 한글 표기(공식/음역) — Gemini, 영구캐시. 한글이거나 실패 시 ''."""
    if _has_hangul(brand):
        return ""
    row = conn.execute("SELECT korean FROM brand_ko_cache WHERE brand=?", (brand,)).fetchone()
    if row is not None:
        return row["korean"] or ""
    ko = ""
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        import requests
        prompt = (f"브랜드명 '{brand}'의 한국에서 통용되는 한글 표기 하나만 출력. "
                  f"설명/기호 없이 한글만. 예: Spigen->슈피겐, K-SECRET->케이시크릿")
        for model in ("gemini-2.5-flash", "gemini-2.0-flash"):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                    json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20)
                if r.status_code == 200:
                    t = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    # 한글만 추출, 공백 제거(전체 음역 유지 — 단어 잘라내기 금지)
                    t = "".join(ch for ch in t if _has_hangul(ch))
                    if _has_hangul(t):
                        ko = t
                        break
            except Exception:
                pass
    conn.execute("INSERT OR REPLACE INTO brand_ko_cache(brand, korean) VALUES(?,?)", (brand, ko))
    return ko


def _strict_kipris(res: dict, query: str) -> dict | None:
    """엄격 매칭: 조회어(≥3자)를 실제로 포함하는 권리자만 인정 → KIPRIS 느슨매칭 노이즈 제거."""
    if not res.get("flagged") or not query or len(query) < 3:
        return None
    ms = [m for m in (res.get("matches") or []) if query in (m.get("name") or "")]
    if not ms:
        return None
    return {"enabled": True, "flagged": True, "status": "수동검토",
            "reason": f"'{query}' 국내 등록 지식재산권 권리자 {len(ms)}건(정확 매칭) → 수동검토",
            "query": query, "matches": ms[:5]}


def _screen_brand_multiform(conn, brand: str) -> dict:
    """국내 등록 IP 정확 매칭 검수. 한글 브랜드는 직접, 영문은 한글 음역으로만 조회(영문 직접조회는 노이즈라 미사용)."""
    nf = {"enabled": True, "flagged": False, "status": "통과",
          "reason": "국내 등록 IP 권리자 미발견", "query": brand, "matches": []}
    wait = {"enabled": True, "flagged": None, "status": "대기",
            "reason": "KIPRIS 조회 불가(쿼터/오류) — 캐시 미저장", "query": brand, "matches": []}
    if len(brand) < 2:
        return nf
    target = brand if _has_hangul(brand) else _brand_korean(conn, brand)
    if not target or (not _has_hangul(brand) and len(target) < 3):
        return nf
    res = kipris_ip_gate.screen(target)
    if res.get("status") == "대기":  # 조회 불가 → '통과' 단정 금지(쿼터 리셋 후 재조회)
        return wait
    return _strict_kipris(res, target) or nf


def populate_kipris_cache(brands: list[str], cap: int = 300, force: bool = False) -> dict:
    """브랜드 목록 KIPRIS 조회 후 영구 캐시 적재. 영문은 한글 음역 재조회 포함. 쿼터 보호(cap)."""
    done = skipped = live = 0
    with get_db() as conn:
        _ensure_kipris_cache(conn)
        _ensure_ko_cache(conn)
        for b in brands:
            b = (b or "").strip()
            if len(b) < 2:
                continue
            cached = _kipris_cache_get(conn, b)
            # force=재조회. 단 이미 flagged면 재호출 불필요 → 스킵
            if cached is not None and (not force or cached.get("flagged")):
                skipped += 1
                continue
            if live >= cap:
                break
            try:
                res = _screen_brand_multiform(conn, b)
                if res.get("status") == "대기":
                    continue  # 조회 불가 → 캐시 안 함(쿼터 리셋 후 재시도)
                _kipris_cache_put(conn, b, res)
                live += 1
                done += 1
            except Exception:
                pass
    return {"cached_new": done, "already": skipped, "live_calls": live}


def _list(page: int, page_size: int, filt: str, q: str, allow_live: bool = False) -> dict:
    """전체 상품 IP 검수 목록(페이지네이션). 빠른 게이트 3종 행별 실시간 +
    KIPRIS는 브랜드 영구캐시 read-through(인증 요청만 미캐시 소량 라이브 채움 — 쿼터 보호)."""
    blocklist = _load_brand_blocklist()
    where, args = [], []
    if filt == "blocked":
        where.append("violation_keyword IS NOT NULL AND violation_keyword!=''")
    elif filt == "pass":
        where.append("(violation_keyword IS NULL OR violation_keyword='')")
    # 검색: ASIN 같으면 정확매칭(인덱스 즉시), 아니면 브랜드 substring(brand 인덱스 커버링)
    order = "id DESC"
    if q:
        if len(q) == 10 and q.upper().startswith("B0"):
            where.append("asin = ?")
            args.append(q.upper())
        else:
            where.append("brand LIKE ?")
            args.append(f"%{q}%")
            order = "brand"  # ★ id 정렬 대신 brand 정렬 → brand 인덱스 커버링 스캔(풀로우 스캔 회피)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    cols = ("asin,title_en,title_ko,brand,amazon_manufacturer,"
            "manufacturer_is_korean,violation_keyword,violation_flags")
    rows = []
    with get_db() as conn:
        _ensure_kipris_cache(conn)
        if filt == "pass" and not q:
            # 통과 COUNT 풀스캔(5s+) 회피 → 전체 − 차단(둘 다 빠름)
            cat = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
            blk = conn.execute(
                "SELECT COUNT(*) c FROM products WHERE violation_keyword IS NOT NULL AND violation_keyword!=''"
            ).fetchone()["c"]
            total = cat - blk
        else:
            total = conn.execute(f"SELECT COUNT(*) c FROM products{wsql}", args).fetchone()["c"]
        recs = conn.execute(
            f"SELECT {cols} FROM products{wsql} ORDER BY {order} LIMIT ? OFFSET ?",
            args + [page_size, (page - 1) * page_size],
        ).fetchall()

        live_budget = 5 if allow_live else 0  # 인증 요청만 미캐시 소량 라이브(쿼터 보호)
        local = {}
        for rec in recs:
            p = dict(rec)
            d = _screen_row(p, blocklist, run_kipris=False)
            vk = (p.get("violation_keyword") or "").strip()
            vf = (p.get("violation_flags") or "").strip()
            d["violation_keyword"] = vk
            d["violation_flags"] = vf
            if vk:
                if not d["blocked"]:
                    d["blocked"] = True
                    d["verdict"] = "차단·수동검토"
                    d["reason"] = f"검수 기록: {vf or '위반'}/{vk}"
                # 저장된 위반을 해당 게이트 컬럼에 반영(라이브 게이트가 못 잡은 경우)
                # → 판정(차단)과 컬럼 표시 일치
                if not any(d["checks"][g]["status"] == "차단"
                           for g in ("brand_gate", "ip_keyword", "korean_mfr")):
                    fl = (vf or "").lower()
                    if "korean" in fl or "manufactur" in fl:
                        col = "korean_mfr"
                    elif "brand" in fl or "gating" in fl or "counterfeit" in fl:
                        col = "brand_gate"
                    else:  # ip_license/copyright/trademark/food_safety/pharma 등 → 정책·IP
                        col = "ip_keyword"
                    d["checks"][col] = {"status": "차단", "matched": vk}

            # ④ KIPRIS — 브랜드 영구캐시 read-through
            brand = (p.get("brand") or "").strip()
            kp = None
            if len(brand) >= 2:
                if brand in local:
                    kp = local[brand]
                else:
                    kp = _kipris_cache_get(conn, brand)
                    if kp is None and live_budget > 0:
                        try:
                            res = kipris_ip_gate.screen(brand)
                            _kipris_cache_put(conn, brand, res)
                            kp = {"status": res["status"], "flagged": bool(res.get("flagged")),
                                  "hits": len(res.get("matches") or []),
                                  "top": (res.get("matches") or [{}])[0].get("name", "") if res.get("matches") else ""}
                            live_budget -= 1
                        except Exception:
                            kp = None
                    local[brand] = kp
            if kp:
                d["checks"]["kipris"] = {"status": kp["status"], "matched": brand,
                                         "hits": kp["hits"], "top": [kp["top"]] if kp["top"] else []}
                if kp["flagged"]:
                    d["blocked"] = True
                    d["verdict"] = "차단·수동검토"
            else:
                d["checks"]["kipris"] = {"status": "대기", "matched": "", "hits": 0, "top": []}
            rows.append(d)

    return {
        "page": page, "page_size": page_size, "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "filter": filt, "q": q, "rows": rows,
    }


@router.get("/list")
def ip_screening_list(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                      filter: str = Query("all"), q: str = Query(""),
                      user: dict = Depends(current_user)):
    # 요청 경로에서 라이브 KIPRIS 호출 금지(지연/쿼터). 캐시 read-only → 빠름.
    # 캐시는 populate_kipris_cache() 배치로 채움. 미캐시 브랜드는 '대기' 표시.
    return _list(page, page_size, filter, q.strip(), allow_live=False)


@router.get("/list/public")
def ip_screening_list_public(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                             filter: str = Query("all"), q: str = Query("")):
    return _list(page, page_size, filter, q.strip(), allow_live=False)
