# -*- coding: utf-8 -*-
"""auction_service.py — 옥션 Open API v1 전송계층 (2026-08-12)

정본: /home/ubuntu/WORKFLOW_DECISIONS.md

★옥션은 ESM 과 별개인 자체 SOAP API 다
    엔드포인트  https://api.auction.co.kr/APIv1/ShoppingService.asmx
    프로토콜    SOAP 1.1 XML Webservice (ASP.NET asmx) · 오퍼레이션 71개
    인증        SOAP 헤더 <EncryptedTicket><Value>216자 티켓</Value></EncryptedTicket>
                네임스페이스 http://www.auction.co.kr/Security

    ESM(sa2.esmplus.com, JWT Bearer)은 **G마켓 전용 경로**다. 옥션 티켓은 거기서 거부된다.
    WSDL 의 'ESM' 언급 5곳은 GetSellingItemList 응답 타입일 뿐 G마켓 등록과 무관하다.

★zeep 을 쓰지 않는다
    운영 서버(t3.small)에 SOAP 라이브러리를 새로 깔지 않았다. 인증 헤더가
    <Value> 하나뿐이고 요청 타입도 얕아 수동 XML 로 충분하다(11번가와 같은 방식).

계정
    old  charisg      (카리스G)
    new  charisglobal (스카이포트 아님 — 11번가와 별개다. 옥션 신계정)
    ★둘 다 216자 티켓 보유. APP_ID 는 판매자 ID 로 보인다.

사용:
  from backend.purchase.services import auction_service as AS
  with AS.auction_account("old"):
      AS.verify_account()
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
import time

import requests

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.auction.co.kr/APIv1/ShoppingService.asmx"

# ★서비스가 셋이다. call(op, ..., service=) 로 고른다.
#   ShoppingService 만 보고 "기능이 없다" 고 판단하지 말 것 — 실제로 여러 번 그렇게 틀렸다.
SERVICES = {
    "shopping": ("https://api.auction.co.kr/APIv1/ShoppingService.asmx",
                 "http://www.auction.co.kr/APIv1/ShoppingService"),
    "auction":  ("https://api.auction.co.kr/APIv1/AuctionService.asmx",
                 "http://www.auction.co.kr/APIv1/AuctionService"),
    "main":     ("http://api.auction.co.kr/ArcheSystem/MainService.asmx",
                 "http://www.auction.co.kr/ArcheSystem/MainService"),
}
NS_TNS = "http://www.auction.co.kr/APIv1/ShoppingService"
NS_SEC = "http://www.auction.co.kr/Security"
NS_SVC = "http://schema.auction.co.kr/Arche.Service.xsd"        # s2
NS_SELL = "http://schema.auction.co.kr/Arche.Sell3.Service.xsd"  # s1

_ctx = threading.local()
_DEFAULT = "new"


class AuctionError(RuntimeError):
    """옥션 API 실패. code 는 SOAP fault code 또는 응답 ResultCode."""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


# ── 계정 ────────────────────────────────────────────────────
@contextlib.contextmanager
def auction_account(account: str):
    """with 블록 안에서만 계정을 바꾼다. 11번가 elevenst_account 와 같은 규약."""
    if account not in ("old", "new"):
        raise ValueError("account 는 old/new 만: %r" % account)
    prev = getattr(_ctx, "account", None)
    _ctx.account = account
    try:
        yield account
    finally:
        _ctx.account = prev


def active_account() -> str:
    return getattr(_ctx, "account", None) or os.getenv("AUCTION_ACTIVE") or _DEFAULT


def _cred(name: str) -> str:
    """★완전 일치로만 고른다 — 채널 식별은 플랫폼+계정 조합이다."""
    key = "AUCTION_%s_%s" % (active_account().upper(), name)
    v = os.getenv(key)
    if not v:
        raise AuctionError("환경변수 %s 없음 (계정 %s)" % (key, active_account()))
    return v


def _ticket() -> str:
    return _cred("AUTH_TICKET")


def seller_id() -> str:
    return _cred("APP_ID")


# ── 전문 ────────────────────────────────────────────────────
def _esc(v) -> str:
    if v is None:
        return ""
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def envelope(op: str, inner: str, service: str = "shopping") -> str:
    """SOAP 1.1 봉투. inner 는 <req>…</req> 본문.

    ★요소는 tns 기본 네임스페이스로 감싼다. asmx 는 elementFormDefault=qualified 라
      하위 요소도 각 스키마 네임스페이스를 따라야 하는 경우가 있어, 실패하면
      _call 이 s2/s1 네임스페이스를 붙인 변형으로 재시도한다.
    """
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Header>'
        '<EncryptedTicket xmlns="%s"><Value>%s</Value></EncryptedTicket>'
        '</soap:Header>'
        '<soap:Body><%s xmlns="%s">%s</%s></soap:Body>'
        # ★본문 네임스페이스도 서비스마다 다르다. SOAPAction 만 바꾸면
        #   .NET 이 req 를 못 만들어 '개체 참조가 …' NullReference 가 난다(실측).
        '</soap:Envelope>' % (NS_SEC, _esc(_ticket()), op, SERVICES[service][1], inner, op)
    )


# ★응답 헤더에 우리가 보낸 티켓이 그대로 에코된다 — 로그·예외에 남으면 자격증명 유출이다.
#   원문을 다루는 모든 경로에서 반드시 mask() 를 거칠 것.
_TICKET_ECHO_RE = re.compile(r"(<Value>)[^<]{40,}(</Value>)")


def mask(text: str) -> str:
    """응답에서 티켓 값을 지운다. 출력·로그·예외 전에 반드시 통과시킨다."""
    return _TICKET_ECHO_RE.sub(r"\1***MASKED***\2", text or "")


_FAULT_RE = re.compile(r"<faultstring>(.*?)</faultstring>", re.S)
_CODE_RE = re.compile(r"<(?:ResultCode|ErrorCode)>(.*?)</", re.S)
_MSG_RE = re.compile(r"<(?:ResultMessage|ErrorMessage|Message)>(.*?)</", re.S)


# ★"인증파라메터의 암호화된 문자열이 유효하지 않습니다" 는 **호출 빈도 제한**이다.
#   자격증명 오류로 위장해서 나온다 — 실측(2026-08-12) 티켓은 멀쩡했고 20초 쉬니 복구됐다.
#   이걸 영구 실패로 기록하면 "티켓 만료" 로 오진한다. 임포트 오퍼 수집 때
#   스로틀을 '오퍼 없음' 으로 오해해 54% 가 가짜였던 것과 같은 함정이다.
_THROTTLE_MARK = "인증파라메터의 암호화된 문자열이 유효하지 않습니다"
_BACKOFF = (2, 5, 12, 25)


def call(op: str, inner: str = "<req/>", timeout: int = 60, service: str = "shopping") -> str:
    """오퍼레이션 호출 → 응답 XML 문자열. 실패면 AuctionError.

    ★빈도 제한은 자동 재시도한다. 그 외 오류는 즉시 올린다 —
      업무 검증 실패를 재시도로 뭉개면 원인을 못 본다.
    """
    for attempt, wait in enumerate((0,) + _BACKOFF):
        if wait:
            time.sleep(wait)
        try:
            return _call_once(op, inner, timeout, service)
        except AuctionError as e:
            if _THROTTLE_MARK not in str(e) or attempt == len(_BACKOFF):
                raise
            logger.warning("옥션 빈도제한 — %s 재시도 %d회차", op, attempt + 1)
    raise AuctionError("도달 불가")


def _call_once(op: str, inner: str, timeout: int, service: str = "shopping") -> str:
    body = envelope(op, inner, service)
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        # ★SOAPAction 의 tns 는 서비스마다 다르다. 어긋나면 조용히 실패한다.
        "SOAPAction": "%s/%s" % (SERVICES[service][1], op),
    }
    r = requests.post(SERVICES[service][0], headers=headers,
                      data=body.encode("utf-8"), timeout=timeout)
    txt = r.text
    f = _FAULT_RE.search(txt)
    if f:
        raise AuctionError("SOAP fault: %s" % f.group(1)[:200], code="fault")
    if r.status_code >= 400:
        raise AuctionError("HTTP %s: %s" % (r.status_code, mask(txt)[:200]), code=str(r.status_code))
    c = _CODE_RE.search(txt)
    if c and c.group(1).strip() not in ("", "0", "00", "S", "Success", "OK"):
        m = _MSG_RE.search(txt)
        raise AuctionError("%s (code=%s)" % ((m.group(1) if m else mask(txt)[:160]), c.group(1)),
                           code=c.group(1))
    return txt


# ── 응답 파싱 ────────────────────────────────────────────────
# ★옥션 응답은 값을 엘리먼트가 아니라 **XML 속성**으로 준다.
#   <ShippingPlaceT ShippingPlaceSeq="146887044" SellerID="charisglob" ... />
#   태그만 찾으면 빈 결과가 나온다.
def rows(xml: str, tag: str) -> list:
    """<tag ... /> 들의 속성을 dict 목록으로."""
    out = []
    for m in re.finditer(r"<%s\s([^>]*?)/?>" % re.escape(tag), xml):
        out.append(dict(re.findall(r'([A-Za-z0-9_]+)="([^"]*)"', m.group(1))))
    return out


# ── 읽기 전용 검증 ───────────────────────────────────────────
def verify_account() -> dict:
    """등록하지 않고 인증만 확인한다. 배송정책 조회를 쓴다(부작용 없음)."""
    inner = ("<req><MemberTicket><Ticket>%s</Ticket></MemberTicket>"
             "<TransPolicyNo>0</TransPolicyNo></req>" % _esc(_ticket()))
    txt = call("GetTransPolicyList", inner)
    n = len(re.findall(r"<TransPolicy[ >]", txt))
    tot = re.search(r"<TotalCount>(\d+)</TotalCount>", txt)
    return {"account": active_account(), "seller_id": seller_id(),
            "trans_policy_count": int(tot.group(1)) if tot else n,
            "raw_len": len(txt)}


def shipping_places() -> list:
    """출고지 목록. ShippingPlaceSeq 가 AddItem 에 필요하다."""
    x = call("GetShippingPlaceCode",
             "<req><MemberTicket><Ticket>%s</Ticket></MemberTicket></req>" % _esc(_ticket()))
    return rows(x, "ShippingPlaceT")


def seller_addresses() -> list:
    x = call("GetSellerAddresses",
             "<req><MemberTicket><Ticket>%s</Ticket></MemberTicket></req>" % _esc(_ticket()))
    return rows(x, "SellerAddr")


def category_attribute(category_code: str, version: int = 1) -> list:
    """★카테고리별 옵션 축. 쿠팡 get_category_meta().attributes 에 해당한다.

    ★요청도 속성 방식이다 — <req CategoryCode="..." Version="1"/>.
      자식 엘리먼트로 보내면 "카테고리 미등록 상품입니다" 로 거부된다(실측 2026-08-12).
      ViewCategoryAttributeRequestT 에는 MemberTicket 이 없다 — 헤더 인증만 쓴다.
    """
    # ★코드는 8자리 왼쪽 0채움이다(7자리 코드는 앞 0이 떨어진 것)
    x = call("ViewCategoryAttribute",
             '<req CategoryCode="%s" Version="%d"/>' % (_esc(str(category_code).zfill(8)), version))
    # ★응답 요소명은 <CategoryAttr> 다 — WSDL 의 타입명 CategoryAttrT 가 아니다.
    #   타입명으로 찾으면 전건 0개가 나오고 "속성 없는 카테고리"로 오독한다(실측 2026-08-12).
    return rows(x, "CategoryAttr") or rows(x, "CategoryAttrT") or rows(x, "Attribute")


# ══════════════════════════════════════════════════════════════════════
#  등록 계층 — AddItem / ReviseItemStock / 판매중지
# ══════════════════════════════════════════════════════════════════════
#
# ★옥션은 상품과 옵션이 **두 오퍼레이션으로 갈린다**
#     AddItem          상품 껍데기(ItemT)          → ItemID 발급
#     ReviseItemStock  옵션·재고(ItemStockT)       → ItemID 를 받아 채운다
#   쿠팡(items[] 한 번에) · 11번가(한 XML 안) 와 다르다. 단품은 1단계로 끝난다.
#
# ★ItemT 는 속성 84개인데 WSDL 에 use="required" 가 **하나도 없다**.
#   필수 집합은 문서로 알 수 없다 — 11번가와 같이 시험 등록 1건으로 알아내야 한다.
#   아래 REQUIRED_GUESS 는 추정이며, 시험 등록 결과로 정정할 것.
#
# ★옵션 축은 3개까지다 (ItemStockTypeCode.ThreeCombination).
#   StockT.@ObjOptClaseNo1/2/3 이 축, @Text/@Text2 가 값, @Price 가 옵션별 추가금.
#   ★@Code 가 판매자 재고코드 = **외부 SKU 필드**다 → 주문 역추적의 연결고리.

# 시험 등록으로 확정할 것 — 지금은 추정이다
REQUIRED_GUESS = (
    "CategoryCode", "Name", "Price", "BuyableQuantity",
    "PlaceOfOrigin", "AfterService", "Description",
)

# 판매상태 (ItemSellingStatusCode)
STATUS_WAIT = "Wait"
STATUS_ONSALE = "OnSale"
STATUS_PAUSED = "Paused"
STATUS_STOP = "StopedBySeller"      # ★kill switch 는 이 값

# 옵션 유형 (ItemStockTypeCode)
STOCK_NONE = "NotAvailable"
STOCK_SELECTIVE = "BuyerSelective"        # 1축 선택형
STOCK_3COMBI = "ThreeCombination"         # ★3축 조합형 — 우리가 쓸 것

# ★OriginTypeCode 열거형이다: Domestic·Imported·Unknown·CoastalWaters·Ocean
#   한글 "수입산" 을 넣으면 .NET 역직렬화가 통째로 깨진다
_ORIGIN_IMPORT = "Imported"


def _attrs(d: dict) -> str:
    """dict → XML 속성 문자열. None 은 뺀다(빈 문자열과 다르다)."""
    out = []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        out.append('%s="%s"' % (k, _esc(v)))
    return " ".join(out)


def normalize_category(code) -> str:
    """★등록용 CategoryCode 는 정확히 8자리다. 왼쪽을 0으로 채운다.

    auction_categories.json 의 7자리 코드는 앞자리 0이 떨어진 것이다
    (5300100 샴푸/린스 ⊂ 5300000 헤어케어 ⊂ 5000000 바디/헤어 → 05… 로 일관).
    ★오른쪽에 0을 붙이면 통과는 하지만 전혀 다른 카테고리가 된다.
    """
    c = str(code or "").strip()
    if not c.isdigit():
        raise AuctionError("카테고리 코드가 숫자가 아니다: %r" % code)
    if len(c) > 8:
        raise AuctionError("카테고리 코드가 8자리를 넘는다: %r" % code)
    return c.zfill(8)



def cut_bytes(text: str, limit: int, enc: str = "euc-kr") -> str:
    """바이트 기준으로 자른다. ★옥션은 글자수가 아니라 **바이트** 제한이다.

    "검색용 상품명은 총합 100Byte를 초과하여 입력할 수 없습니다" 를 실제로 맞았다.
    한글은 EUC-KR 2바이트라 100자를 넣으면 200바이트가 된다.
    """
    if not text:
        return text
    b = text.encode(enc, "ignore")
    if len(b) <= limit:
        return text
    return b[:limit].decode(enc, "ignore")


# ★관부가세 미포함 배송정책. GetTransPolicyList 로 확인한 값(DutyIncludedYn=N).
#   지정하지 않으면 옥션이 기본정책(2134945, Duty 설정 없음)을 배정할 수 있다.
DUTY_FREE_POLICY_NO = 2134939


# ── 수입정보·안전인증 (ItemExtra) ────────────────────────────────
NATION_US = "68"                 # GetNationCode 실측. 240 은 '기타국가'
SAFE_GROUPS = ("Child", "Electric", "Life", "Harmful")


def item_extra_xml(*, nation=NATION_US, import_agency="미국",
                   safety_sign="BuyingAgent", cert_type="NotCert") -> str:
    """<ItemExtra> — 안 보내면 옥션이 기본값(미상·기타국가)을 넣는다.

    ★safety_sign 은 UnknownOrNone | ParallelImport | **BuyingAgent** 중 하나다.
      우리는 구매대행이므로 BuyingAgent 가 사실이다. 안 보내면 '미상'으로 남는다.
    ★cert_type 은 기본이 NotCert(인증 대상 아님)이며 이미 그렇게 들어가고 있었다.
      KC 대상 판정은 M12~M14 가 하고 m19 게이트가 막는다 — 여기서 판정하지 않는다.
    """
    groups = "".join(
        '<IntegrateSafeCertGroupList MandatorySafetySign="%s" CertificationType="%s"'
        ' CertificationGroupNo="%s"/>' % (safety_sign, cert_type, g)
        for g in SAFE_GROUPS)
    return ('<ItemExtra xmlns="%s">'
            '<ImportedItem Nation="%s" ImportAgency="%s" IsMultiple="false"/>'
            '<IntegrateSafeCert ChangeType="Add">%s</IntegrateSafeCert>'
            '</ItemExtra>' % (NS_SVC, _esc(str(nation)), _esc(import_agency), groups))


def build_item(*, category_code, name, price, qty, description_html,
               shipping_place_seq, seller_addr_no,
               brand_name=None, origin=_ORIGIN_IMPORT, after_service=None,
               images=None, trans_policy_no=None, seller_stock_code=None,
               is_adult=False, extra=None, return_info=None,
               shipping_policy_no=None, barcode=None,
               advertise_message=None, market_price=None) -> str:
    """<Item .../> 조립. **공식 전문샘플 기준** (2026-08-13 재작성).

    ★Description 은 빈 값이어야 한다 — 공식 설명: "description 내용 추가시 입력안됩니다".
      상세는 ItemContentsHtml 의 세 속성으로만 들어간다.
    ★자식 요소는 NS_SVC(Arche.Service.xsd) 다. Item(NS_SELL=Sell3) 과 다르다.
    ★배송비는 무료(사장님 방침) — ShippingFeeChargeType="Free".

    return_info  {"zip","addr","street","tel","mobile","fee","notice"} — ItemReturn 용.
                 샘플 3종에 모두 들어 있어 넣는 것을 기본으로 한다.
    """
    a = {
        "CategoryCode": normalize_category(category_code),
        "Name": (name or "")[:100],
        # ★검색용 상품명은 100**바이트** 제한이다(글자수 아님). 명시하지 않으면
        #   서버가 Name 을 그대로 써서 한글 상품명이 바로 초과한다.
        "NameSearch": cut_bytes(name or "", 90),
        "Price": int(price),
        "BuyableQuantity": int(qty),
        "PlaceOfOrigin": origin,
        "AfterService": after_service or "판매자 연락처로 문의",
        "BrandName": brand_name,
        # ★빈 값이어야 한다. HTML 을 넣으면 상세가 통째로 무시된다(공식 설명).
        "Description": "",
        "DescriptionVerType": "New",   # ★New 만 지원. Old 를 넣으면 "String null" 이 난다
        "ItemStatusType": "New",
        "SellingArea": "Nationwide",
        "IsAdult": bool(is_adult),
        "SingleItemYn": "Y" if not extra or not extra.get("has_option") else "N",
        # ★ItemCode = 판매자 관리코드. 종전엔 이 값을 BarCode 에 넣어
        #   ASIN 이 바코드로 등록되고 있었다(방침 위반). barcode 는 별도 인자로만 받는다.
        "ItemCode": seller_stock_code,
        # ★공식 전문샘플에서 확인한 자리들 (2026-08-13)
        "AdvertiseMessage": advertise_message or None,   # 프로모션 문구
        "AfterService": after_service or None,
        "MarketPrice": market_price or None,             # 정가(할인 표시 기준)
        "BarCode": barcode or None,
        # 브랜드 마스터 조회 메서드가 API 색인에 없다 → 직접입력으로 채운다
        #   (BrandName 만 보내면 BrandCode=-1 미지정으로 남는다)
        "UserDefineBrandName": brand_name,
        "IsVATFree": False,
        "MinBuyQty": 1,
        "BuyLimitTypeCode": "Unlimited",
    }
    if extra:
        a.update({k: v for k, v in extra.items() if k != "has_option"})

    parts = ['<Item xmlns="%s" %s>' % (NS_SELL, _attrs(a))]

    # ── 배송비 — 무료 ──
    # ★TransPolicyNo 는 ShippingT 의 **속성**이다(WSDL 확인). 종전 주석의
    #   "존재하지 않는다" 는 틀렸다 — 자식 요소로 넣어 무시된 것을 오해한 것이다.
    #   ★관부가세 포함 여부(DutyIncludedYn)가 이 정책에 달려 있다. 안 보내면
    #   옥션이 기본정책을 배정해 상품마다 관부가세 표시가 달라진다.
    ship_a = {"ShippingType": "Door2Door", "IsPrepayable": True,
              "FeeFreeConditionType": "Discount", "ShippingFeeChargeType": "Free",
              "TransPolicyNo": int(trans_policy_no or DUTY_FREE_POLICY_NO)}
    parts.append('<ShippingFee xmlns="%s" %s>' % (NS_SVC, _attrs(ship_a)))
    # ★SellerShipping 을 쓰면 묶음배송 정책(ShippingPolicyNo)까지 요구한다 —
    #   우리는 출고지별 정책이 없어 20001 "묶음 배송 정보가 존재하지 않습니다" 가 난다.
    #   ItemShipping(상품별) + 출고지 + Free 조합이 실측으로 통과한 형태다.
    parts.append("<ShipingFeeType>ItemShipping</ShipingFeeType>")     # ★'Shiping' 오타 아님
    parts.append("<ShippingPlaceSeq>%d</ShippingPlaceSeq>" % int(shipping_place_seq))
    if shipping_policy_no:
        parts.append("<ShippingPolicyNo>%d</ShippingPolicyNo>" % int(shipping_policy_no))
    parts.append("</ShippingFee>")
    # ★공식 샘플에는 ShippingFee 다음 형제로 <ShippingPlace> 가 있다. 우리는 안 보내고 있었다.
    parts.append('<ShippingPlace xmlns="%s" %s/>' % (NS_SVC, _attrs({
        "ShippingPlaceSeq": int(shipping_place_seq),
        "SellerAddrNo": int(seller_addr_no) if seller_addr_no else None})))

    # ── 반품지 — 샘플 3종에 모두 있다 ──
    if return_info:
        r = return_info
        parts.append('<ItemReturn xmlns="%s" DeliveryAgency="%s">'
                     % (NS_SVC, _esc(r.get("agency") or "gmgls")))
        parts.append('<Address %s/>' % _attrs({
            "ZipCode": r.get("zip"), "Address": r.get("addr"), "Street": r.get("street")}))
        parts.append('<ExtraInfo %s/>' % _attrs({
            "MobileTel": r.get("mobile"), "ReturnTel": r.get("tel"),
            "ReturnFee": r.get("fee"), "ReturnNotice": r.get("notice")}))
        parts.append("</ItemReturn>")

    # ── 상세 — 여기가 유일한 입력 경로다 ──
    parts.append('<ItemContentsHtml xmlns="%s" %s/>' % (NS_SVC, _attrs({
        "ItemHtml": description_html or "", "ItemPromotionHtml": "", "ItemAddHtml": ""})))

    # ── 이미지 — Picture1~15 (샘플에 ListingPicture 는 없다) ──
    if images:
        pics = []
        for i, im in enumerate(images[:15], start=1):
            pics.append('<Picture%d %s/>' % (i, _attrs({"Uri": im[0]})))
        parts.append('<ItemPicture xmlns="%s">%s</ItemPicture>' % (NS_SVC, "".join(pics)))

    # ★수입정보·안전인증 — 안 보내면 '미상 / 기타국가' 로 등록된다(2026-08-13 실측)
    parts.append(item_extra_xml())
    parts.append("</Item>")
    return "".join(parts)


def add_item(item_xml: str, *, allow_live: bool = False, version: int = 1) -> dict:
    """★실제로 옥션에 상품이 올라간다. allow_live=True 를 명시해야 나간다.

    11번가 시험 등록 때 자동 중지가 실패해 상품이 라이브로 남았다.
    호출자는 반드시 반환된 item_id 로 stop_selling() 을 호출할 것.
    """
    if not allow_live:
        raise AuctionError("allow_live=True 없이 실등록 불가 — 대외 노출 행위다")
    inner = ('<req Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket>%s</req>'
             % (version, _esc(_ticket()), item_xml))
    x = call("AddItem", inner)
    # ★응답도 속성이다. 11번가에서 <prdNo> 를 찾다 놓친 것과 같은 함정.
    m = re.search(r'<AddItemResponseT?\s[^>]*ItemID="([^"]+)"', x) \
        or re.search(r'\bItemID="([^"]+)"', x)
    lim = rows(x, "DisplayLimit") or rows(x, "DisplayLimitCheckT")
    return {"item_id": m.group(1) if m else None,
            "display_limit": lim[0] if lim else None,
            "raw": mask(x)}


def stop_selling(item_id: str, *, status: str = STATUS_STOP, version: int = 1) -> bool:
    """판매중지. ★시험 등록 직후 반드시 호출한다."""
    inner = ('<req Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket>'
             '<ItemSellingStatus ItemID="%s" Status="%s"/></req>'
             % (version, _esc(_ticket()), _esc(item_id), _esc(status)))
    x = call("ReviseItemSellingStatus", inner)
    m = re.search(r'\bStatus="(true|false)"', x)
    return bool(m and m.group(1) == "true")


def view_item(item_id: str, version: int = 1) -> dict:
    """등록된 상품 원문. ★필수 필드 역추출에 쓴다 — 한 건만 올려두면 정본이 된다."""
    inner = ('<req ItemID="%s" Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket></req>'
             % (_esc(item_id), version, _esc(_ticket())))
    x = call("ViewItem", inner)
    it = rows(x, "Item")
    return {"item": it[0] if it else None, "raw": mask(x)}


def selling_status(item_id: str, version: int = 1) -> str:
    """현재 판매상태. 중지가 실제로 먹었는지 **독립 경로로** 확인한다."""
    inner = ('<req ItemID="%s" Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket></req>'
             % (_esc(item_id), version, _esc(_ticket())))
    x = call("ViewItemSelling", inner)
    m = re.search(r'\bStatus="([A-Za-z]+)"', x)
    return m.group(1) if m else "?"


# ── 옵션(2단계) ──────────────────────────────────────────────
def build_stock(item_id: str, axis_names: list, combos: list, *,
                stock_type: str = STOCK_3COMBI) -> str:
    """<ItemStock .../> 조립.

    axis_names  ["색상","사이즈"]  최대 3
    combos      [{"values":["레드","L"], "price":0, "qty":10, "code":"B0XXXX"}, ...]
                price 는 **대표가 대비 추가금**이다(절대가가 아니다).
                ★code 가 외부 SKU — child ASIN 을 넣어 주문 역추적을 연다.
    """
    if len(axis_names) > 3:
        raise AuctionError("옥션 축 상한 3 초과: %d" % len(axis_names))
    names = {}
    for i, nm in enumerate(axis_names[:5], start=1):
        names["ClaseName%d" % i] = nm
    head = _attrs({"ItemID": item_id, "Type": stock_type,
                   "OptionStockType": "Available", "IsStockQtyMng": True})
    parts = ["<ItemStock %s>" % head,
             "<OptionObjectName %s/>" % _attrs(names)]
    for c in combos:
        v = list(c.get("values") or [])
        parts.append("<OrderStock %s/>" % _attrs({
            "Text": v[0] if len(v) > 0 else None,
            "Text2": v[1] if len(v) > 1 else None,
            "Price": int(c.get("price", 0)),
            "Quantity": int(c.get("qty", 0)),
            "StockQty": int(c.get("qty", 0)),
            "Code": c.get("code"),
            "IsDisplayable": True,
            "ChangeType": "Add",
        }))
    parts.append("</ItemStock>")
    return "".join(parts)


def revise_item_stock(stock_xml: str, *, allow_live: bool = False, version: int = 1) -> dict:
    if not allow_live:
        raise AuctionError("allow_live=True 없이 옵션 반영 불가")
    inner = ('<req Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket>%s</req>'
             % (version, _esc(_ticket()), stock_xml))
    x = call("ReviseItemStock", inner)
    return {"ok": True, "raw": mask(x)}


# ── 2~4단계 (2026-08-13, 공식 전문샘플 기준) ────────────────────────────────
# 등록은 4단계다. AddItem 만 하면 상태가 Waiting 이고 노출되지 않는다.

def notice_items(group_no: int, version: int = 1) -> list:
    """고시 그룹의 항목 목록. [(code, name, essential), ...]

    ★노트에 '그룹번호를 모른다'로 막혀 있던 지점이다 — 이 함수로 뚫린다.
      11번가와 같은 법정 분류라 15=자동차용품 · 18=화장품 · 25=스포츠용품.
    """
    inner = ('<req Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket>'
             '<NotiItemGroupNo xmlns="%s">%d</NotiItemGroupNo></req>'
             % (version, _esc(_ticket()), NS_SELL, int(group_no)))
    x = call("GetOfficialNoticeInfo", inner)
    # ★속성명은 NotiItemCodeName 이다(NotiItemName 아님)
    return re.findall(
        r'NotiItemCode="([^"]*)"\s+NotiItemCodeName="([^"]*)"\s+EssenIs="([^"]*)"', x)


# 고시 값 규칙 — 11번가에서 확립한 것을 그대로 쓴다.
# ★코드가 그룹 종속(15-1 …)이라 **항목명으로** 맞춘다.
_NOTI_RULES = [
    (re.compile(r"품\s*명|모델\s*명"), "_TITLE_"),
    (re.compile(r"제조자|수입자|판매업자|제조업자"), "_BRAND_"),
    (re.compile(r"제조국|원산지"), "미국"),
    (re.compile(r"품질\s*보증"), "관련법 및 소비자분쟁해결기준에 따름"),
    (re.compile(r"배송\s*기간|배송기간"), "결제 후 7~14일 이내"),
]


def notice_values(group_no: int, *, title: str, brand: str, skip_999: bool = True) -> list:
    """[(code, value), ...] — 그룹 항목을 우리 규칙으로 채운다.

    ★999(거래정보)는 2014-03-31 부터 선택이라 기본으로 건너뛴다.
    """
    out = []
    for code, name, _ess in notice_items(group_no):
        if skip_999 and code.startswith("999"):
            continue
        val = "상품상세설명 참조"
        for rx, v in _NOTI_RULES:
            if rx.search(name):
                val = {"_TITLE_": (title or "")[:50], "_BRAND_": brand or "상품상세설명 참조"}.get(v, v)
                break
        out.append((code, val))
    return out


def add_official_notice(item_id: str, group_no: int, values, version: int = 1) -> str:
    """2단계. values = [(NotiItemCode, NotiItemValue), ...]

    ★<ItemID>·<NotiItemGroupNo>·<ItemOfficialNotiValue> 는 NS_SVC 다.
      바깥 <ItemOfficialNotice> 만 NS_SELL — AddItem 과 같은 두 겹 구조다.
    """
    vals = "".join(
        '<ItemOfficialNotiValue NotiItemCode="%s" NotiItemValue="%s" ExtraMarkIs="false" xmlns="%s"/>'
        % (_esc(c), _esc(v), NS_SVC) for c, v in values)
    inner = ('<req Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket>'
             '<ItemOfficialNotice xmlns="%s">'
             '<ItemID xmlns="%s">%s</ItemID>'
             '<NotiItemGroupNo xmlns="%s">%d</NotiItemGroupNo>'
             '%s</ItemOfficialNotice></req>'
             % (version, _esc(_ticket()), NS_SELL, NS_SVC, _esc(item_id),
                NS_SVC, int(group_no), vals))
    return mask(call("AddOfficialNotice", inner))


def revise_item_selling(item_id: str, *, status: str = "OnSale",
                        apply_period: int = None, version: int = 1) -> str:
    """4단계. status = OnSale | Stop

    ★판매중지도 이 함수다. ReviseItemSellingStatus 가 아니다 —
      그걸 쓰다가 "상품번호가 유효하지 않습니다" 로 막혔다.
    """
    body = ('<Period ApplyPeriod="%d"/>' % int(apply_period)) if apply_period else ""
    inner = ('<req Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket>'
             '<ItemSelling ItemID="%s" xmlns="%s">'
             '<Period Status="%s" xmlns="%s">%s</Period>'
             '</ItemSelling></req>'
             % (version, _esc(_ticket()), _esc(item_id), NS_SELL,
                _esc(status), NS_SVC, body))
    return mask(call("ReviseItemSelling", inner))


def view_item_stock(item_id: str, version: int = 1) -> list:
    """현재 재고 행. [{StockNo, Section, Text, Quantity, Price}, ...]

    ★req 는 **속성** 형태다: <req ItemID="…" Version="1">.
      자식 <ItemID> 로 보내면 "유효한 물품이 아니거나 등록된 물품이 아닙니다" 가 난다.
    """
    inner = ('<req ItemID="%s" Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket></req>'
             % (_esc(item_id), version, _esc(_ticket())))
    x = call("ViewItemStock", inner)
    out = []
    # ★단독형(1축) 재고는 <StockStandAlone> 로 온다. OrderStock 만 읽으면
    #   옵션을 제대로 넣고도 "반영 안 됨" 으로 오판한다(2026-08-13 실측).
    for tag in ("OrderStock", "StockStandAlone"):
        for m in re.finditer(r"<%s\b([^>]*)/?>" % tag, x):
            a = m.group(1)
            g = lambda k: (re.search(r'%s="([^"]*)"' % k, a) or [None, ""])[1]
            out.append({"kind": tag,
                        "StockNo": g("StockNo") or g("ItemStockStandAloneNo"),
                        "Section": g("Section"), "Text": g("Text"),
                        "Quantity": g("Quantity") or g("StockQty"),
                        "Price": g("Price"),
                        "Code": g("Code") or g("SellerStockCode")})
    return out


def set_stock_quantity(item_id: str, qty: int, *, seller_stock_code: str = None,
                       version: int = 1) -> dict:
    """3단계. 기존 재고 행의 수량을 올린다.

    ★AddItem 이 만든 재고는 수량 0 이라 그대로면 품절이다.
    ★seller_stock_code(@Code)에 child ASIN 을 심으면 주문 역추적이 된다 —
      스마트스토어에서 옵션ID 를 안 남겨 오배송 4건이 났던 그 구멍을 여기서 막는다.
    """
    # ★재고는 2단계다. AddItem 은 재고 행을 만들지 않는다.
    #   자식 없이 ReviseItemStock 을 한 번 보내면 행이 생긴다(Quantity=0).
    rows = view_item_stock(item_id, version)
    if not rows:
        init = ('<req Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket>'
                '<ItemStock ItemID="%s" Type="NotAvailable" OptionStockType="NotAvailable"'
                ' UseOptionBuyQty="false" OptVerType="New" ImageMatchingFinishYN="false"'
                ' xmlns="%s"/></req>'
                % (version, _esc(_ticket()), _esc(item_id), NS_SELL))
        call("ReviseItemStock", init)
        time.sleep(5)
        rows = view_item_stock(item_id, version)
    if not rows:
        raise AuctionError("재고 행 생성 실패 — AddItem 이 정상인지 확인할 것")
    kids = []
    for r in rows:
        attrs = {"StockNo": r["StockNo"], "Section": r["Section"] or "_",
                 "Text": r["Text"] or "_", "Quantity": int(qty), "Price": 0,
                 "IsDisplayable": True, "ChangeType": "Update"}
        if seller_stock_code:
            attrs["Code"] = seller_stock_code
        kids.append('<OrderStock %s xmlns="%s"/>' % (_attrs(attrs), NS_SVC))
    inner = ('<req Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket>'
             '<ItemStock ItemID="%s" Type="NotAvailable" OptionStockType="NotAvailable"'
             ' UseOptionBuyQty="false" OptVerType="New" ImageMatchingFinishYN="false"'
             ' xmlns="%s">%s</ItemStock></req>'
             % (version, _esc(_ticket()), _esc(item_id), NS_SELL, "".join(kids)))
    call("ReviseItemStock", inner)
    # ★성공 응답 ≠ 반영. 독립 조회로 확인한다(자식 없이 보내면 조용히 안 바뀐 전례가 있다)
    after = view_item_stock(item_id, version)
    return {"before": rows, "after": after,
            "ok": any(str(r.get("Quantity")) not in ("0", "") for r in after)}


def revise_item_pictures(item_id: str, image_urls, version: int = 1) -> str:
    """이미지만 교체한다. ★옥션에는 상품 삭제 API 가 없어 이 경로가 유일한 교정 수단이다.

    ★첫 URL 이 대표(ListingPicture)다 — import_image 는 반드시 rep 우선으로 정렬해서 넘길 것.
      `ORDER BY slot` 은 알파벳순이라 "features3" < "rep" 가 되어 설치 도식이 대표가 된다(실측).

    구조는 WSDL 에서 읽었다(개발자 사이트 샘플 탭이 비어 있다):
        <ItemImage ItemID="…" xmlns=NS_SELL><ItemPicture xmlns=NS_SVC>…
    """
    if not image_urls:
        raise AuctionError("이미지가 없다")
    pics = ['<ListingPicture Uri="%s"/>' % _esc(image_urls[0])]
    for i, u in enumerate(image_urls[:15], start=1):
        pics.append('<Picture%d Uri="%s"/>' % (i, _esc(u)))
    inner = ('<req Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket>'
             '<ItemImage ItemID="%s" xmlns="%s"><ItemPicture xmlns="%s">%s</ItemPicture>'
             '</ItemImage></req>'
             % (version, _esc(_ticket()), _esc(item_id), NS_SELL, NS_SVC, "".join(pics)))
    return mask(call("ReviseItemPictures", inner))


# ── 그룹(주문선택형 옵션) ────────────────────────────────────────────
# ★옥션은 축을 최대 2개까지만 Section/Text 로 받는다(OptionObjectName 이 ClaseName1·2).
#   3축 이상이면 여기서 막고 M9.4a(축 줄이기)로 돌려보낸다 — 조용히 버리면 안 된다.
# 축 수 → ItemStock Type (WSDL ItemStockTypeCode 실측)
AUCTION_MAX_AXIS = 3
_STOCK_TYPE = {1: "StandAlone", 2: "BuyerSelective", 3: "ThreeCombination"}


def set_option_stock(item_id: str, axes: list, rows: list, *, qty: int = 100,
                     version: int = 1) -> dict:
    """주문옵션을 등록한다. 축 수에 따라 전문 형태가 완전히 달라진다.

    axes  ["사이즈"] 또는 ["색상", "사이즈"]            최대 3
    rows  [{"values": [...], "price": 추가금, "code": child ASIN}, ...]

    ★1축(StandAlone)  <StockStandAlone Section=축이름 Text=값 SellerStockCode StockQty>
    ★2·3축(조합형)     <OrderStock Section=값1 Text=값2 [Text2=값3] Code Quantity>
      Section 의 의미가 서로 반대다. 헷갈리면 조용히 축 이름이 값으로 들어간다.
    """
    if not axes:
        raise AuctionError("축이 없다 — 단품은 set_stock_quantity 를 쓸 것")
    if len(axes) > AUCTION_MAX_AXIS:
        raise AuctionError("옥션 축 상한 %d 초과 (%d개: %s) — M9.4a 로 축을 줄일 것"
                           % (AUCTION_MAX_AXIS, len(axes), ", ".join(axes)))
    if not rows:
        raise AuctionError("옵션 행이 없다")
    itype = _STOCK_TYPE[len(axes)]

    oa = {}
    for i, ax in enumerate(axes, 1):
        oa["ClaseName%d" % i] = cut_bytes(ax, 50)   # ★ClaseName. ClassName 아니다
        oa["ObjOptNo%d" % i] = 0                    # 0 으로 통과한다(실측)
    kids = ['<OptionObjectName %s xmlns="%s"/>' % (_attrs(oa), NS_SVC)]

    seen = set()
    for r in rows:
        vals = [cut_bytes(v, 50) for v in (r.get("values") or [])]
        if len(vals) != len(axes):
            raise AuctionError("축 수와 값 수가 다르다: %r" % (vals,))
        key = tuple(vals)
        if key in seen:
            raise AuctionError("옵션 조합 중복: %s" % " / ".join(vals))
        seen.add(key)
        price = int(r.get("price") or 0)
        if len(axes) == 1:
            a = {"Section": cut_bytes(axes[0], 50), "Text": vals[0], "Price": price,
                 "StockQty": int(qty), "UseYN": True, "ChangeType": "Add"}
            if r.get("code"):
                a["SellerStockCode"] = r["code"]
            kids.append('<StockStandAlone %s xmlns="%s"/>' % (_attrs(a), NS_SVC))
        else:
            a = {"Section": vals[0], "Text": vals[1], "Quantity": int(qty),
                 "Price": price, "IsDisplayable": True, "ChangeType": "Add"}
            if len(vals) > 2:
                a["Text2"] = vals[2]
            if r.get("code"):
                a["Code"] = r["code"]      # ★child ASIN. 주문 역추적의 유일한 고리
            kids.append('<OrderStock %s xmlns="%s"/>' % (_attrs(a), NS_SVC))

    inner = ('<req Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket>'
             '<ItemStock ItemID="%s" Type="%s" OptionStockType="NotAvailable"'
             ' UseOptionBuyQty="false" OptVerType="New" ImageMatchingFinishYN="false"'
             ' xmlns="%s">%s</ItemStock></req>'
             % (version, _esc(_ticket()), _esc(item_id), itype, NS_SELL, "".join(kids)))
    call("ReviseItemStock", inner)
    # ★성공 응답 ≠ 반영. 되읽어 우리가 보낸 코드가 실제로 박혔는지 본다.
    after = view_item_stock(item_id, version)
    want = {r["code"] for r in rows if r.get("code")}
    got = {x.get("Code") for x in after if x.get("Code")}
    return {"type": itype, "sent": len(rows), "after": after,
            "missing": sorted(want - got), "ok": bool(want) and want <= got}


def delete_stock_rows(item_id: str, stock_nos: list, *, version: int = 1) -> str:
    """재고 행 삭제. 시험 중 남은 잔재 행을 치울 때 쓴다.

    ★ChangeTypeCode 는 Add · Update · **Remove** 다. Delete 를 쓰면
      "XML 문서(1, N)에 오류가 있습니다" 만 나오고 어느 필드인지 안 알려준다.
    """
    kids = "".join('<OrderStock StockNo="%s" ChangeType="Remove" xmlns="%s"/>'
                   % (_esc(str(n)), NS_SVC) for n in stock_nos)
    inner = ('<req Version="%d"><MemberTicket><Ticket>%s</Ticket></MemberTicket>'
             '<ItemStock ItemID="%s" Type="NotAvailable" OptionStockType="NotAvailable"'
             ' UseOptionBuyQty="false" OptVerType="New" xmlns="%s">%s</ItemStock></req>'
             % (version, _esc(_ticket()), _esc(item_id), NS_SELL, kids))
    return mask(call("ReviseItemStock", inner))
