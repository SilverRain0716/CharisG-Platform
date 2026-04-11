"""
sheets_service.py — Google Sheets 연동
기존 크롤러/리스터와 동일한 시트에서 데이터 읽기
"""
import logging
from typing import Optional
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

import os
from backend_shared._config import SHEET_ID, GOOGLE_SA_KEY_PATH

logger = logging.getLogger(__name__)

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

_client: Optional[gspread.Client] = None


def _get_client() -> gspread.Client:
    global _client
    if _client is None:
        sa_path = Path(GOOGLE_SA_KEY_PATH)
        if not sa_path.exists():
            raise FileNotFoundError(f"서비스 계정 파일 없음: {sa_path}")
        creds = Credentials.from_service_account_file(str(sa_path), scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client


def get_winning_candidates(sheet_name: str = "CJ_위닝후보") -> list[dict]:
    """
    CJ_위닝후보 시트에서 상품 목록 조회
    기존 smartstore_lister.py의 load_candidates_from_sheets()와 동일한 시트
    """
    if not SHEET_ID:
        logger.warning("SHEET_ID 미설정 — 빈 목록 반환")
        return []

    try:
        client = _get_client()
        sheet = client.open_by_key(SHEET_ID)
        ws = sheet.worksheet(sheet_name)
        rows = ws.get_all_records()

        items = []
        for i, row in enumerate(rows):
            # PID: 상품URL에서 pid= 파라미터 추출
            url = str(row.get("상품URL", ""))
            cj_pid = ""
            if "pid=" in url:
                cj_pid = url.split("pid=")[-1].split("&")[0]

            # 리스팅상태 → status 매핑
            raw_status = str(row.get("리스팅상태", row.get("상태", "대기중"))).strip()
            status = _normalize_status(raw_status)

            # 소싱가($) → 판매가(원) 계산 (마크업 2.8 × 환율 1400)
            sourcing = _parse_float(row.get("소싱가($)", row.get("소싱가", "")))
            sale_krw = int(sourcing * 2.8 * 1400 / 100) * 100 if sourcing else None

            items.append({
                "row_index": i + 2,
                "product_name": str(row.get("상품명", "")).strip(),
                "cj_pid": cj_pid,
                "category": str(row.get("카테고리", "")).strip(),
                "sourcing_usd": sourcing,
                "sale_price_krw": sale_krw,
                "status": status,
                "naver_product_id": str(row.get("스마트스토어ID", "")).strip() or None,
            })

        logger.info("Sheets에서 %d개 상품 로드 (시트: %s)", len(items), sheet_name)
        return items

    except Exception as e:
        logger.error("Sheets 조회 실패: %s", e)
        return []


def get_sheet_stats(sheet_name: str = "CJ_위닝후보") -> dict:
    """KPI용 집계"""
    items = get_winning_candidates(sheet_name)
    total = len(items)
    registered = sum(1 for x in items if x["status"] == "등록됨")
    pending = sum(1 for x in items if x["status"] == "대기중")
    failed = sum(1 for x in items if x["status"] == "실패")

    return {
        "total": total,
        "registered": registered,
        "pending": pending,
        "failed": failed,
        "analyzing": total - registered - pending - failed,
    }


def _normalize_status(raw: str) -> str:
    """다양한 상태값을 표준화"""
    r = raw.lower().strip()
    if r in ("등록완료", "등록됨", "registered", "listed"):
        return "등록됨"
    if r in ("실패", "failed", "error"):
        return "실패"
    if r in ("분석중", "analyzing"):
        return "분석중"
    return "대기중"


def _parse_float(val) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(str(val).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_int(val) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(float(str(val).replace("원", "").replace(",", "").strip()))
    except (ValueError, TypeError):
        return None
