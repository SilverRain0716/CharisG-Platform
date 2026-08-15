"""elevenst_test_register.py — 11번가 실등록 1건 시도 (검증용).

목적
----
1. `EP_PRODUCT` 가 문서값일 뿐 실등록으로 검증된 적이 없다. 실제로 통하는지 본다.
2. `global_dlv=0` 카테고리(해외배송 불가 표시)에 구매대행 상품을 올릴 수 있는지 본다.
   막힌다면 후보를 4,893개로 좁혀야 하고, 통과한다면 12,314개 전부 쓸 수 있다.
   이 답에 따라 카테고리 매퍼의 후보 범위가 갈린다.

안전장치
--------
· 등록 성공하면 **즉시 판매중지**(PUT /prodstatservice/stat/stopdisplay/[prdNo]).
  실 스토어에 물건이 걸린 채로 남지 않게 한다. --keep 으로 끌 수 있다.
· --dry-run 이면 전문만 만들어 출력하고 호출하지 않는다.

사용:
    PYTHONPATH=<repo> .venv/bin/python -m backend.purchase.scripts.elevenst_test_register \\
        --product-id N --category 1020704 [--account new] [--dry-run] [--keep]
"""
import argparse
import os
import re
import xml.etree.ElementTree as ET

from dotenv import load_dotenv

_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))

import logging

from backend.purchase.database import get_db
from backend.purchase.services import elevenst_service as ES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("11st-test")

EP_STOP = "/rest/prodstatservice/stat/stopdisplay/"


def build_payload(p: dict, category: str, tmplt_no: str) -> str:
    """상품등록 XML. 일반 공산품 단품 기준 최소 필드.

    ★조건부 필수는 넣지 않는다 — hsCode(11번가 해외배송 쓸 때만),
      의료기기 허가번호, 축산물 이력번호, 식품 원재료/함량 등.
      우리는 dlvCstInstBasiCd=01(무료)이라 hsCode 가 빠진다.
    """
    def esc(v):
        return ES.xml_escape(v if v is not None else "")

    title = (p["title_ko"] or p["title_en"] or "")[:100]
    price = int(p["sale_price_krw"] or 0)
    price = max(1000, price - price % 10)          # 10원 단위
    img = p["image_url"] or ""
    brand = (p["brand"] or "")[:50]

    f = [
        ("selMethCd", None),                        # (미사용 — 자리표시)
    ]
    del f

    parts = [
        f"<selMthdCd>01</selMthdCd>",               # 고정가판매
        # ★문서는 aplBgnDy 를 선택(X)이라 하지만 없으면 500 "판매시작일 누락" 이다.
        #   selTermUseYn=N(기간 설정 안 함 = 영구판매)으로 요구를 없앤다.
        f"<selTermUseYn>N</selTermUseYn>",
        f"<dispCtgrNo>{esc(category)}</dispCtgrNo>",
        f"<prdTypCd>01</prdTypCd>",                 # 일반배송상품
        f"<prdNm><![CDATA[{title}]]></prdNm>",
        f"<prdStatCd>01</prdStatCd>",               # 새상품
        f"<minorSelCnYn>Y</minorSelCnYn>",
        f"<suplDtyfrPrdClfCd>01</suplDtyfrPrdClfCd>",   # 과세
        f"<selPrc>{price}</selPrc>",
        # ★문서는 X(선택)이지만 없으면 "상품재고 수량이 0개" 로 거부된다. 0 도 불가.
        f"<prdSelQty>100</prdSelQty>",
        f"<prdImage01>{esc(img)}</prdImage01>",
        f"<htmlDetail><![CDATA[{title} 상세]]></htmlDetail>",
        # 해외 구매대행
        f"<forAbrdBuyClf>01</forAbrdBuyClf>",        # 일반판매상품
        f"<abrdBuyPlace>D</abrdBuyPlace>",           # 현지 온라인 쇼핑몰
        f"<orgnTypCd>02</orgnTypCd>",                # 해외
        # ★orgnTypCd=02(해외)면 상세지역 코드가 필수다. 코드표: i.011st.com/openapi/area.xlsx
        #   1405 = 미국.
        f"<orgnTypDtlsCd>1405</orgnTypDtlsCd>",
        # 배송 — 01(무료)이라 hsCode 불필요
        f"<dlvCstInstBasiCd>01</dlvCstInstBasiCd>",
        f"<dlvCnAreaCd>01</dlvCnAreaCd>",            # 전국
        # ★asDetail·rtngExchDetail 은 문서상 O 이고 실제로도 필수. 비우면 안 되고
        #   내용이 없으면 '.' 이라도 넣어야 한다(공백 불가).
        "<asDetail><![CDATA[해외 구매대행 상품입니다. 상품 문의는 고객센터로 연락 바랍니다.]]></asDetail>",
        "<rtngExchDetail><![CDATA[해외 배송 특성상 단순 변심 반품 시 왕복 배송비가 부과됩니다.]]></rtngExchDetail>",
        f"<rtngdDlvCst>5000</rtngdDlvCst>",
        f"<exchDlvCst>10000</exchDlvCst>",
        # 상품정보제공고시
        # ★상품정보제공고시 — 템플릿 번호만으로는 "상품고시항목이 입력되지 않았습니다",
        #   항목을 대충 넣으면 "고시 항목 개수가 일치하지 않습니다" 로 거부된다.
        #   유형(type)에 정의된 항목을 **전부** 넣어야 한다. 값은 미상이어도
        #   '상품상세설명 참조' 로 채우는 게 관행.
        #   891035 = 스포츠용품(12항목). 항목코드는 셀러오피스 고시항목 팝업 기준(2026-08-11).
        "<ProductNotification><type>891035</type>"
        + "".join(
            f"<item><code>{c}</code><name><![CDATA[{v}]]></name></item>"
            for c, v in [
                ("11800", title[:50]),              # 품명 및 모델명 ★유형 헤더 행에
                #   숨어 있어 빠뜨리기 쉽다. 빠지면 "항목 개수가 일치하지 않습니다".
                ("11835", "상품상세설명 참조"),      # 색상
                ("11900", "상품상세설명 참조"),      # 재질
                ("11905", brand or "상품상세설명 참조"),   # 제조자/수입자
                ("17461", "상품상세설명 참조"),      # 제품구성
                ("23760454", "상세설명 참조"),       # KC 인증정보
                ("23759095", "미국"),               # 제조국
                ("23759938", "상품상세설명 참조"),    # 동일모델 출시년월
                ("23760223", "상품상세설명 참조"),    # 크기, 중량
                ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),
                ("23760437", "상품상세설명 참조"),    # A/S 책임자와 전화번호
                ("23756377", "상품상세설명 참조"),    # 상품별 세부 사양
            ])
        + "</ProductNotification>",
        # ★brand 는 조건부가 아니라 사실상 필수다. 비우면
        #   "브랜드코드가 없습니다. <apiPrdAttrBrandCd/> 없는 경우 <brand/> 명 필수"
        #   로 500 이 떨어진다(실측). 우리 DB 브랜드가 비면 노브랜드로 채운다.
        f"<brand><![CDATA[{brand or '노브랜드'}]]></brand>",
    ]
    body = "".join(parts)
    return f'<?xml version="1.0" encoding="EUC-KR"?><Product>{body}</Product>'


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--product-id", type=int, required=True)
    ap.add_argument("--category", required=True, help="11번가 dispCtgrNo")
    ap.add_argument("--account", default="new", choices=("old", "new"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep", action="store_true", help="등록 후 판매중지하지 않음")
    args = ap.parse_args()

    with get_db() as conn:
        p = conn.execute(
            "SELECT id, asin, title_ko, title_en, brand, sale_price_krw, images_json "
            "FROM products WHERE id=?", (args.product_id,)).fetchone()
        if not p:
            raise SystemExit(f"product {args.product_id} 없음")
        cat = conn.execute(
            "SELECT disp_no, full_path, global_dlv, is_leaf FROM elevenst_categories "
            "WHERE disp_no=?", (args.category,)).fetchone()

    if not cat:
        raise SystemExit(f"카테고리 {args.category} 없음")
    logger.info("카테고리 %s · leaf=%s · 해외배송=%s · %s",
                cat["disp_no"], cat["is_leaf"], cat["global_dlv"], cat["full_path"])

    import json as _json
    imgs = []
    try:
        imgs = _json.loads(p["images_json"] or "[]")
    except Exception:
        pass
    row = dict(p)
    row["image_url"] = imgs[0] if imgs else ""
    if not row["image_url"]:
        raise SystemExit("대표 이미지가 없다 — prdImage01 은 필수")

    tmplt = ES.prd_info_tmplt_no(None)
    xml = build_payload(row, args.category, tmplt)
    logger.info("전문 %d바이트\n%s", len(xml), xml[:600])

    if args.dry_run:
        logger.info("DRY-RUN — 호출하지 않음")
        return

    with ES.elevenst_account(args.account):
        ES.verify_account(force=True)
        logger.info("[%s] 등록 호출", args.account)
        try:
            res = ES.register_product(xml)
        except ES.ElevenstError as e:
            logger.error("등록 실패: %s (code=%s)", e, getattr(e, "code", None))
            return
        logger.info("등록 응답: %s", res)

        prd_no = None
        if isinstance(res, dict):
            prd_no = res.get("prdNo") or res.get("productNo")
        if prd_no and not args.keep:
            logger.info("즉시 판매중지 (prdNo=%s)", prd_no)
            try:
                ES._request("PUT", EP_STOP + str(prd_no), timeout=30)
                logger.info("판매중지 완료")
            except Exception as e:
                logger.error("★판매중지 실패 — 수동 확인 필요: %s", e)


if __name__ == "__main__":
    main()
