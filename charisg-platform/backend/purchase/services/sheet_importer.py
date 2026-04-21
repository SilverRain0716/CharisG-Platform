"""Google Sheets (공개 시트) → sourcing_candidates 일괄 import.

사용자 흐름:
  1) Claude 웹 프로젝트(v3.1 프롬프트)가 키워드별 시트 탭에 enriched ASIN 리스트 출력
  2) 사용자가 '링크가 있는 모든 사용자: 뷰어' 로 공유 상태 변경
  3) PA Sourcing 페이지에 시트 URL paste
  4) 이 모듈이 모든 탭 발견 + CSV fetch + INSERT OR IGNORE

시트는 *공개* 상태여야 한다. 실패하면 PERMISSION_DENIED 로 상위에 던진다.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from typing import Optional

import requests

from backend.purchase.database import get_db

logger = logging.getLogger("pa.sheet_importer")

SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

# htmlview HTML 내부의 탭 메타는 자바스크립트 리터럴 형태:
#   items.push({name: "ASIN_ram", pageUrl: "...gid=904263828", gid: "904263828",...});
# 실제 htmlview 응답을 2026-04-11 확인하여 패턴 확정.
# 구조가 바뀔 가능성에 대비해 대체 패턴 2개를 추가로 시도한다.
TAB_PATTERNS = [
    re.compile(r'items\.push\(\{name:\s*"([^"]+)"[^}]*gid:\s*"(\d+)"'),
    re.compile(r'\{"name"\s*:\s*"([^"]+)"\s*,\s*"gid"\s*:\s*"?(\d+)"?'),
    re.compile(r'name\s*:\s*"([^"]+)"[^}]{0,200}?gid\s*[:=]\s*"?(\d+)"?'),
]

HEADER_ALIASES = {
    "asin":          ["asin", "ASIN"],
    "title":         ["상품명", "title", "product_name", "product"],
    "amazon_url":    ["상품 url", "amazon_url", "url", "상품URL", "상품url"],
    "price_usd":     ["가격($)", "가격", "price", "price_usd", "가격(USD)"],
    "price_krw":     ["가격(KRW)", "가격(원)", "price_krw", "금액(KRW)", "가격(W)"],
    "rating":        ["별점", "rating", "stars"],
    "review_count":  ["리뷰수", "reviews", "review_count", "리뷰 수"],
    "monthly_sales": ["월판매량", "monthly_sales", "월 판매량"],
    "category":      ["카테고리", "category"],
    "notes":         ["가 (특이사항)", "특이사항", "비고", "notes"],
    "image_url":     ["이미지", "이미지 URL", "image", "image_url", "대표이미지", "thumbnail"],
}


def extract_sheet_id(url: str) -> str:
    m = SHEET_ID_RE.search(url or "")
    if not m:
        raise ValueError("올바른 Google Sheets URL 이 아닙니다")
    return m.group(1)


def _fetch(url: str, timeout: int = 20) -> requests.Response:
    return requests.get(url, timeout=timeout, allow_redirects=True)


def discover_tabs(sheet_id: str) -> list[dict]:
    """공개 시트의 모든 탭 (gid + name) 목록 반환.

    htmlview 가 첫 번째 탭만 담는 경우가 있어서, 여러 패턴을 시도한다.
    모두 실패하면 fallback 으로 gid=0 만 반환한다.
    """
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/htmlview"
    r = _fetch(url)
    if r.status_code == 404:
        raise PermissionError("시트가 존재하지 않습니다")
    if r.status_code != 200:
        raise PermissionError("시트가 비공개이거나 존재하지 않습니다")
    # 일부 비공개 시트는 200 + 로그인 페이지를 돌려주기도 한다
    body = r.text
    if "ServiceLogin" in body or "accounts.google.com" in body and "signin" in body.lower():
        raise PermissionError("시트가 비공개 상태입니다. '링크가 있는 모든 사용자: 뷰어' 로 공유하세요")

    tabs: list[dict] = []
    seen: set[int] = set()
    for pat in TAB_PATTERNS:
        for m in pat.finditer(body):
            groups = m.groups()
            if len(groups) != 2:
                continue
            # 패턴 3은 (gid, name) 순서라 판별
            if groups[0].isdigit() and not groups[1].isdigit():
                gid = int(groups[0])
                name = groups[1].strip()
            else:
                name = groups[0].strip()
                try:
                    gid = int(groups[1])
                except ValueError:
                    continue
            if gid in seen:
                continue
            seen.add(gid)
            tabs.append({"name": name, "gid": gid})
        if tabs:
            break

    if not tabs:
        logger.warning("discover_tabs: no tabs matched — fallback to gid=0")
        tabs = [{"name": "Sheet1", "gid": 0}]
    return tabs


def fetch_tab_csv(sheet_id: str, gid: int) -> list[dict]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    r = _fetch(url, timeout=30)
    if r.status_code != 200:
        return []
    text = r.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _pick(row: dict, keys: list[str]) -> str:
    # 헤더 대소문자/공백/내부 줄바꿈 차이를 흡수 ("가격\n(KRW)" → "가격(krw)")
    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", s or "").lower()
    normalized = {_norm(k): v for k, v in row.items()}
    for k in keys:
        v = normalized.get(_norm(k))
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _parse_price(raw: str) -> tuple[Optional[float], Optional[str]]:
    """값 → (float, currency). currency ∈ {'USD','KRW',None}.

    프리픽스/suffix 감지: '₩', 'KRW', '원' → KRW / '$', 'USD' → USD.
    감지 실패 시 currency=None (호출자가 헤더 기반으로 해석).
    """
    s = (raw or "").strip()
    if not s:
        return None, None
    s_upper = s.upper()
    cur: Optional[str] = None
    if s.startswith("₩") or s_upper.startswith("KRW") or s.endswith("원"):
        cur = "KRW"
    elif s.startswith("$") or s_upper.startswith("USD"):
        cur = "USD"
    cleaned = s.replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not m:
        return None, cur
    try:
        return float(m.group()), cur
    except ValueError:
        return None, cur


def parse_row(row: dict) -> Optional[dict]:
    """시트 한 행 → sourcing_candidates INSERT 용 dict. 필수 누락 시 None."""
    asin = _pick(row, HEADER_ALIASES["asin"]).upper()
    if not ASIN_RE.match(asin):
        return None

    price_usd: Optional[float] = None
    price_krw: Optional[float] = None

    krw_raw = _pick(row, HEADER_ALIASES["price_krw"])
    if krw_raw:
        price_krw, _ = _parse_price(krw_raw)

    usd_raw = _pick(row, HEADER_ALIASES["price_usd"])
    if usd_raw:
        val, cur = _parse_price(usd_raw)
        if cur == "KRW":
            # 헤더가 '가격' 등 범용인데 값에 KRW/₩ 마커가 있으면 KRW 로 분류
            if price_krw is None:
                price_krw = val
        else:
            price_usd = val

    review_str = _pick(row, HEADER_ALIASES["review_count"]).replace(",", "")
    reviews = int(review_str) if review_str.isdigit() else 0

    rating_str = _pick(row, HEADER_ALIASES["rating"])
    try:
        rating = float(rating_str) if rating_str else None
    except ValueError:
        rating = None

    amazon_url = _pick(row, HEADER_ALIASES["amazon_url"]) or f"https://www.amazon.com/dp/{asin}"

    return {
        "asin": asin,
        "title": _pick(row, HEADER_ALIASES["title"]),
        "amazon_url": amazon_url,
        "price_usd": price_usd,
        "price_krw": price_krw,
        "rating": rating,
        "review_count": reviews,
        "monthly_sales": _pick(row, HEADER_ALIASES["monthly_sales"]) or None,
        "category": _pick(row, HEADER_ALIASES["category"]) or None,
        "notes": _pick(row, HEADER_ALIASES["notes"]) or None,
        "image_url": _pick(row, HEADER_ALIASES["image_url"]) or None,
    }


def import_rows(rows: list[dict]) -> tuple[int, int]:
    """sourcing_candidates 에 INSERT OR IGNORE. (imported, skipped) 반환."""
    imported = 0
    skipped = 0
    with get_db() as conn:
        for r in rows:
            cur = conn.execute(
                """INSERT OR IGNORE INTO sourcing_candidates
                   (asin, title, amazon_url, price_usd, price_krw, rating, review_count,
                    monthly_sales, category, notes, image_url, sourcing_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered')""",
                (
                    r["asin"], r["title"], r["amazon_url"],
                    r["price_usd"], r.get("price_krw"),
                    r["rating"], r["review_count"], r["monthly_sales"],
                    r["category"], r["notes"], r.get("image_url"),
                ),
            )
            if cur.rowcount > 0:
                imported += 1
            else:
                skipped += 1
    return imported, skipped


def import_from_sheet_url(sheet_url: str) -> dict:
    sheet_id = extract_sheet_id(sheet_url)
    try:
        tabs = discover_tabs(sheet_id)
    except PermissionError as e:
        return {"error": "PERMISSION_DENIED", "message": str(e)}

    result_tabs = []
    total_imported = 0
    total_skipped = 0
    for tab in tabs:
        rows = fetch_tab_csv(sheet_id, tab["gid"])
        parsed = [p for p in (parse_row(r) for r in rows) if p]
        imported, skipped = import_rows(parsed)
        total_imported += imported
        total_skipped += skipped
        result_tabs.append({
            "name": tab["name"],
            "gid": tab["gid"],
            "imported": imported,
            "skipped": skipped,
        })

    return {
        "tabs": result_tabs,
        "total_imported": total_imported,
        "total_skipped": total_skipped,
    }
