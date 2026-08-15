"""식품 카테고리 오매핑 상품 재분류 — 식품→비식품 고신뢰 건을 올바른 카테고리로 이동.

사용자 지시(2026-05-25): 필수속성 파일의 상품들이 엉뚱한(식품) 카테고리에 매핑돼
엉뚱한 필수속성을 요구함. 분류기로 재분류해 식품→비식품 고신뢰 건만 카테고리 교정.

대상: /tmp/req_attrs.xlsx 中 현재 '식품' 카테고리 상품.
판정: find_coupang_category_with_gemini(기본 init, 강제힌트 없음).
      top!=식품 AND score>=90 AND not needs_review  → 진짜 오매핑(=교정 대상).
교정(상품당):
  1) get_category_meta(새코드) → build_required_attributes / build_default_notices 로 새 속성 구성.
     skip_reason 이면 필수속성 못 채움 → 카테고리 변경 보류(건너뜀, de-list 회피).
  2) get_seller_product → displayCategoryCode=새코드, items[].attributes/notices=새값 → PUT
  3) 임시저장 전환 대기 → request_approval(재노출).
멱등: GET 결과 카테고리가 이미 새코드면 skip. 이미 올바른 식품 상품은 top==식품 → 대상 아님.

실행:
  .venv/bin/python -m backend.purchase.scripts.recategorize_food --test          # 20건 read-only
  nohup .venv/bin/python -m backend.purchase.scripts.recategorize_food --apply > /tmp/recat.log 2>&1 &
"""
import argparse
import logging
import os
import sqlite3
import sys
import time

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")
XLSX = os.environ.get("REQ_ATTRS_XLSX", "/tmp/req_attrs.xlsx")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("recat")


def top(path):
    p = str(path or "")
    return p.split("]")[-1].split(">")[0].strip() if "]" in p else p.split(">")[0].strip()


# 카테고리 경로 키워드 → 고시(noticeCategoryName) 매핑. '기타 재화'가 없을 때만 사용.
# ※ 도서(서적)는 쿠팡이 '서적' 고시 전송을 거부(ISBN 기반 별도 처리) → 매핑 없음 = 안전 스킵.
NOTICE_KEYWORD_MAP = []


def pick_notices(meta, cat_path, fallback="상품 상세페이지 참조"):
    """새 카테고리에 맞는 고시 페이로드 선택.
    ① '기타 재화'(범용) 우선 — 기존 리스팅이 쓰던 스키마라 항상 통과.
    ② 없으면 경로 키워드 매칭(도서→서적 등).
    ③ 둘 다 실패 → None (호출부가 해당 상품 스킵, 잘못된 고시 전송 방지).
    빈 리스트 반환 = 고시 불필요 카테고리.
    """
    cats = meta.get("noticeCategories") or []
    by_name = {}
    for c in cats:
        if isinstance(c, dict) and c.get("noticeCategoryName"):
            by_name[c["noticeCategoryName"]] = c
    if not by_name:
        return []
    chosen = None
    if "기타 재화" in by_name:
        chosen = "기타 재화"
    else:
        for keys, target in NOTICE_KEYWORD_MAP:
            if any(k in (cat_path or "") for k in keys) and target in by_name:
                chosen = target
                break
    if not chosen:
        return None
    result = []
    for d in by_name[chosen].get("noticeCategoryDetailNames") or []:
        dn = d.get("noticeCategoryDetailName") if isinstance(d, dict) else d
        if dn:
            result.append({"noticeCategoryName": chosen, "noticeCategoryDetailName": dn, "content": fallback})
    return result


def load_food_rows():
    import openpyxl
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["템플릿"]
    rows = [r for r in ws.iter_rows(min_row=7, values_only=True) if r[0] and r[0] != "Inventory ID"]
    return [r for r in rows if "식품" in str(r[6] or "")]


def put_category(cs, cpid, new_code, attributes, notices):
    """GET → displayCategoryCode + items[].attributes/notices 교체 → PUT. (skip/put_ok/fail, msg)"""
    info = cs.get_seller_product(str(cpid))
    if not info:
        return "fail", "조회실패"
    data = info.get("data")
    if not isinstance(data, dict):
        return "fail", "data없음"
    if str(data.get("displayCategoryCode")) == str(new_code):
        return "skip", "이미새카테고리"
    items = data.get("items") or []
    if not items:
        return "fail", "items없음"
    data["displayCategoryCode"] = int(new_code)
    for it in items:
        it["attributes"] = attributes
        it["notices"] = notices
    path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
    r = cs._request_with_retry("PUT", cs.BASE + path, headers=cs._signature("PUT", path), json=data, timeout=30)
    if r is None:
        return "fail", "no response"
    body = r.json() if r.text else {}
    if r.status_code < 400 and isinstance(body, dict) and body.get("code") != "ERROR":
        return "put_ok", ""
    return "fail", f"status={r.status_code} " + "; ".join(cs._extract_error_messages(body))[:140]


def main(test, apply, limit=0):
    from backend.purchase.services.category_mapper import find_coupang_category_with_gemini as fc
    from backend.purchase.services.coupang_meta import get_category_meta, extract_notice_category_names
    from backend.purchase.services.coupang_attributes import build_required_attributes
    from backend.purchase.services import coupang_service as cs

    food = load_food_rows()
    logger.info(f"[1] 필수속성 파일 식품 카테고리 상품: {len(food)}건")
    if test:
        step = max(1, len(food) // 20)
        food = food[::step][:20]
        logger.info(f"[1t] 테스트 샘플 {len(food)}건 (read-only)")
    elif limit > 0:
        food = food[:limit]
        logger.info(f"[1L] 앞 {len(food)}건만 처리 (검증용 --limit)")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    n_target = n_attr_ok = n_attr_skip = n_food_stays = n_lowconf = n_err = 0
    put_ok = appr = put_skip = put_fail = stuck = 0
    stuck_ids = []
    for r in food:
        cpid = str(r[0]); name = str(r[1] or "")
        pr = con.execute(
            "SELECT * FROM products p JOIN listings_pa l ON l.product_id=p.id "
            "WHERE l.channel='coupang' AND l.channel_product_id=? LIMIT 1", (cpid,),
        ).fetchone()
        pdict = dict(pr) if pr else {}
        te = pdict.get("title_en") or ""
        try:
            res = fc(name, sample_en=te)
        except Exception as e:
            n_err += 1
            logger.warning(f"  cpid={cpid} 분류오류: {e}")
            continue
        new_code = str(res.get("code") or "")
        new_path = res.get("path") or ""
        st = top(new_path)
        if not st or not new_code:
            n_err += 1
            continue
        if st == "식품":
            n_food_stays += 1
            continue
        if res.get("score", 0) < 90 or res.get("needs_review"):
            n_lowconf += 1
            continue
        # 진짜 오매핑 — 새 카테고리 속성 구성
        n_target += 1
        meta = get_category_meta(new_code)
        if not meta:
            n_attr_skip += 1
            logger.warning(f"  cpid={cpid} | {name[:28]} → {new_path[:40]} | 메타조회 실패 → 보류")
            continue
        attrs, skip_reason = build_required_attributes(meta, pdict, cat_path=new_path)
        if skip_reason:
            n_attr_skip += 1
            logger.info(f"  cpid={cpid} | {name[:28]} → {new_path[:40]} | 속성보류: {skip_reason}")
            continue
        notices = pick_notices(meta, new_path)
        if notices is None:
            n_attr_skip += 1
            logger.info(f"  cpid={cpid} | {name[:28]} → {new_path[:40]} | 고시선택 실패(안전스킵): {extract_notice_category_names(meta)}")
            continue
        n_attr_ok += 1
        if test:
            logger.info(f"  ✓ cpid={cpid} | {name[:28]} → {new_path[:46]} (score {res.get('score')}) | attrs={len(attrs)} notices={len(notices)}")
            continue
        if not apply:
            continue
        # 실제 PUT
        pst, perr = put_category(cs, cpid, new_code, attrs, notices)
        if pst == "skip":
            put_skip += 1
            continue
        if pst != "put_ok":
            put_fail += 1
            if put_fail <= 25:
                logger.warning(f"  cpid={cpid} PUT 실패: {perr}")
            continue
        put_ok += 1
        a_ok = False
        for _ in range(4):
            time.sleep(2)
            a_ok, aerr = cs.request_approval(str(cpid))
            if a_ok:
                break
        if a_ok:
            appr += 1
            logger.info(f"  ✓ {cpid} → {new_path[:46]} 재분류+재승인 OK")
        else:
            stuck += 1
            stuck_ids.append(cpid)

    logger.info(f"[2] 분류 결과 — 진짜오매핑={n_target} (속성OK={n_attr_ok} 속성보류={n_attr_skip}) "
                f"식품유지={n_food_stays} 저신뢰={n_lowconf} 오류={n_err}")
    if apply and not test:
        logger.info(f"[3] 적용 결과 — PUT={put_ok} 재승인={appr} skip(이미새카테고리)={put_skip} "
                    f"fail={put_fail} ★임시저장잔류={stuck}")
        if stuck_ids:
            logger.warning(f"★임시저장 잔류 {len(stuck_ids)}건 (reapprove_stuck sweep 필요): {stuck_ids[:60]}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="20건 read-only (쿠팡 변경 없음)")
    ap.add_argument("--apply", action="store_true", help="실제 카테고리 PUT")
    ap.add_argument("--limit", type=int, default=0, help="apply 시 앞 N건만 (검증용)")
    args = ap.parse_args()
    try:
        main(args.test, args.apply, args.limit)
    except Exception:
        logger.exception("=== 예외 ===")
        sys.exit(1)
