"""
coupang_required_attrs_autofill.py — 쿠팡 [필수 속성 일괄 입력] 엑셀 자동 채우기.

입력: 쿠팡에서 다운받은 '필수 속성 제공' 엑셀 (시트 '템플릿', row 7+ 데이터)
출력: 같은 형식의 엑셀, 입력 가능 셀(속성값/옵션값)에 자동 추정값 채움

채움 전략:
  - col 11 (속성값1, GTIN): 비움 (우리 데이터 없음)
  - col 13 (속성값2, 품번): ASIN
  - col 15 (옵션값1): col 14 옵션유형 보고 title에서 weight 또는 volume 파싱
  - col 17 (옵션값2): col 16 옵션유형 보고 title에서 용량/수량 파싱
  - col 19 (옵션값3): col 18 옵션유형 보고 수량 파싱 (기본 1)

옵션유형 → 단위 매핑:
  '개당 중량' / '최소 중량' → g (oz × 28.3495, lb × 453.592)
  '개당 용량'              → ml (fl oz × 29.5735)
  '개당 캡슐/정'           → 정 (count)
  '수량'                    → 개

사용:
  .venv/bin/python3 -m backend.purchase.scripts.coupang_required_attrs_autofill \\
      --in /tmp/필수_속성_260501.xlsx --out /tmp/필수_속성_260501_filled.xlsx
"""
import argparse
import logging
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import openpyxl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 단위 변환
OZ_TO_G = 28.3495
LB_TO_G = 453.592
FLOZ_TO_ML = 29.5735
FLOZ_TO_ML_UK = 28.4131  # 미국 fl oz 기준 사용


# ─ title 파싱 ─

def parse_weight_g(title: str) -> int | None:
    """title 에서 weight 추출 → g 단위 정수."""
    if not title:
        return None
    t = title.lower()
    # 'X oz' / 'X.X oz' (단, fl oz 는 제외)
    m = re.search(r"(\d+\.?\d*)\s*oz\b(?!\s*(\(|\.|fl))", t.replace("fl oz", "##FLOZ##"))
    if m:
        try: return int(float(m.group(1)) * OZ_TO_G)
        except: pass
    # 'X lb' / 'X lbs'
    m = re.search(r"(\d+\.?\d*)\s*lbs?\b", t)
    if m:
        try: return int(float(m.group(1)) * LB_TO_G)
        except: pass
    # 'X g' / 'X gram' (3자리 이상이어야 의미있음 → 노이즈 방지)
    m = re.search(r"(\d{2,4}\.?\d*)\s*g(ram)?s?\b", t)
    if m:
        try: return int(float(m.group(1)))
        except: pass
    # 'X kg'
    m = re.search(r"(\d+\.?\d*)\s*kg\b", t)
    if m:
        try: return int(float(m.group(1)) * 1000)
        except: pass
    return None


def parse_volume_ml(title: str) -> int | None:
    """title 에서 volume 추출 → ml 단위 정수."""
    if not title:
        return None
    t = title.lower()
    # 'X fl oz' (us)
    m = re.search(r"(\d+\.?\d*)\s*fl\.?\s*oz\b", t)
    if m:
        try: return int(float(m.group(1)) * FLOZ_TO_ML)
        except: pass
    # 'X ml'
    m = re.search(r"(\d+\.?\d*)\s*ml\b", t)
    if m:
        try: return int(float(m.group(1)))
        except: pass
    # 'X l' / 'X liter'
    m = re.search(r"(\d+\.?\d*)\s*l(iter)?s?\b", t)
    if m:
        try:
            v = float(m.group(1))
            if v < 100:  # 100 이상이면 다른 의미일 가능성
                return int(v * 1000)
        except: pass
    return None


def parse_capsules(title: str) -> int | None:
    """캡슐/정 수 — 'X capsules' 'X tablets' 'X count' 'X ct' 'X softgels' 패턴."""
    if not title:
        return None
    t = title.lower()
    patterns = [
        r"(\d{2,5})\s*(capsules?|caps?|softgels?|tablets?|gummies|gummy)\b",
        r"(\d{2,5})\s*(?:ct|count)\b",
        r"\((\d{2,5})\s*(?:capsules?|caps?|tablets?|softgels?|count|ct|pack)\b",
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            try: return int(m.group(1))
            except: pass
    return None


def parse_quantity(title: str) -> int | None:
    """수량 (Pack of N, N pack, N count) — 캡슐과 별도."""
    if not title:
        return None
    t = title.lower()
    # 'pack of X'
    m = re.search(r"pack\s*of\s*(\d{1,4})", t)
    if m:
        try: return int(m.group(1))
        except: pass
    # 'X-pack' / 'X pack' (1~5 단위 small)
    m = re.search(r"\b(\d{1,3})[-\s]?pack\b", t)
    if m:
        try: return int(m.group(1))
        except: pass
    # set/box/bag
    m = re.search(r"\b(\d{1,3})[-\s]?(?:bottles?|boxes?|bags?|cans?|jars?|tubes?)\b", t)
    if m:
        try: return int(m.group(1))
        except: pass
    return None


# ─ 옵션유형 분류 ─

def classify_option_type(opt_type: str | None) -> str | None:
    """옵션유형 셀 → 'weight_g' / 'volume_ml' / 'capsules' / 'count' / None."""
    if not opt_type:
        return None
    t = opt_type
    if "중량" in t and "기본단위: g" in t:
        return "weight_g"
    if "용량" in t and ("기본단위: ml" in t or "ml" in t):
        return "volume_ml"
    if "캡슐" in t or "정" in t:
        return "capsules"
    if "수량" in t:
        return "count"
    return None


def fill_value(opt_type: str | None, title: str) -> tuple[int | None, str]:
    """(value, source) — value=None 이면 채우지 못함."""
    kind = classify_option_type(opt_type)
    if kind == "weight_g":
        v = parse_weight_g(title)
        return v, "weight"
    if kind == "volume_ml":
        v = parse_volume_ml(title)
        return v, "volume"
    if kind == "capsules":
        v = parse_capsules(title) or parse_quantity(title)
        return v, "capsules"
    if kind == "count":
        v = parse_quantity(title) or 1  # default 1
        return v, "count"
    return None, "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--out", dest="out_path", required=True)
    args = ap.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path)
    if not in_path.exists():
        logger.error(f"파일 없음: {in_path}")
        sys.exit(1)

    logger.info(f"열기: {in_path}")
    wb = openpyxl.load_workbook(str(in_path))
    ws = wb["템플릿"]

    # DB lookup
    db = Path(__file__).resolve().parents[1] / "purchase.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    # 행 단위 처리 (row 8+ = data; openpyxl 1-indexed)
    n_rows = 0
    n_품번 = 0
    n_w = 0
    n_v = 0
    n_c = 0
    n_q = 0
    n_missed = 0
    missed_examples = []

    for row in ws.iter_rows(min_row=8, values_only=False):
        seller_product_id = row[0].value
        if not seller_product_id:
            continue
        n_rows += 1

        # listings_pa → product_id → products
        lst = conn.execute(
            "SELECT product_id FROM listings_pa WHERE channel='coupang' AND channel_product_id=? LIMIT 1",
            (str(seller_product_id),),
        ).fetchone()
        product = None
        if lst:
            product = conn.execute(
                "SELECT id, asin, title_en, title_ko, weight_g FROM products WHERE id=?",
                (lst["product_id"],),
            ).fetchone()

        title_en = product["title_en"] if product else (row[1].value or "")
        title_ko = product["title_ko"] if product else ""
        title = f"{title_en} {title_ko}".strip()
        asin = product["asin"] if product else None
        db_weight_g = product["weight_g"] if product else None

        # col 13 (속성값2 - 품번): ASIN
        if asin:
            row[13].value = asin
            n_품번 += 1

        # col 15 (옵션값1): col 14 옵션유형 기준
        opt_type_1 = row[14].value
        v1, src1 = fill_value(opt_type_1, title)
        # weight 일 경우 DB 값 우선
        if classify_option_type(opt_type_1) == "weight_g" and db_weight_g:
            v1 = db_weight_g
        if v1 is not None:
            row[15].value = v1
            if src1 == "weight": n_w += 1
            elif src1 == "volume": n_v += 1
            elif src1 == "capsules": n_c += 1
            elif src1 == "count": n_q += 1
        else:
            n_missed += 1
            if len(missed_examples) < 5 and opt_type_1:
                missed_examples.append((seller_product_id, opt_type_1[:30], title[:60]))

        # col 17 (옵션값2): col 16 옵션유형 기준
        opt_type_2 = row[16].value
        v2, _ = fill_value(opt_type_2, title)
        if classify_option_type(opt_type_2) == "weight_g" and db_weight_g and not v2:
            v2 = db_weight_g
        if v2 is not None:
            row[17].value = v2

        # col 19 (옵션값3): col 18 옵션유형 기준 — 보통 '수량'
        opt_type_3 = row[18].value
        v3, _ = fill_value(opt_type_3, title)
        if v3 is not None:
            row[19].value = v3

    logger.info(f"처리 완료: {n_rows}행")
    logger.info(f"  품번(ASIN) 채움: {n_품번}")
    logger.info(f"  중량(g) 채움: {n_w}")
    logger.info(f"  용량(ml) 채움: {n_v}")
    logger.info(f"  캡슐/정 채움: {n_c}")
    logger.info(f"  수량 채움: {n_q}")
    logger.info(f"  옵션값1 미채움: {n_missed}")
    if missed_examples:
        logger.info("  미채움 샘플:")
        for sid, ot, ti in missed_examples:
            logger.info(f"    spid={sid} type={ot} | {ti}")

    wb.save(str(out_path))
    logger.info(f"저장: {out_path}")


if __name__ == "__main__":
    main()
