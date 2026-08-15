"""
elevenst_service.py — 11번가 오픈 API (4번째 판매 채널).

전문이 XML + EUC-KR 이고 인증은 헤더 `openapikey` 하나뿐이다. JSON/UTF-8 인
쿠팡·네이버와 전송 계층에서 공유할 게 없어 따로 둔다.
EC2 의존: ELEVENST_API_KEY .env + IP 화이트리스트(52.78.174.31).

★2026-08-11 2계정화 — 쿠팡·네이버와 같은 contextvar 패턴을 이식했다.
`with elevenst_account("old"):` 블록 안의 모든 호출이 그 계정으로 라우팅된다.
컨텍스트를 안 쓰면 ELEVENST_ACTIVE(기본 new)로 동작해 종전과 같다(회귀 0).

**폴백은 여전히 만들지 않는다** — 자격증명이 비면 조용히 다른 계정 값을 잡는 대신
호출 시점에 ElevenstError 로 죽는다. (naver_cfg/coupang_cfg 의 `or` 폴백은 이름에
오타가 나면 다른 계정 자격증명이 새는 함정이 있다. 여기서는 답습하지 않는다.)

계정: new = 카리스 글로벌(스카이포트, memNo 76614773)
      old = 카리스G(memNo 68232815)
"""
import contextvars
import logging
import re
import threading
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from typing import Optional
from xml.sax.saxutils import escape as _xml_escape

import requests
from requests.adapters import HTTPAdapter

from backend.purchase.database import get_db
from backend_shared._config import ELEVENST_ACTIVE, elevenst_cfg
from backend_shared.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

BASE = "https://api.11st.co.kr"
ENC = "euc-kr"          # 요청·응답 양방향. UTF-8 로 보내면 한글이 깨진 채 등록된다.
CHANNEL = "elevenst"    # listings_pa.channel 값

# ── HTTP Session (Connection Pool) ────────────────────────────
_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
_SESSION.mount("https://", _adapter)
_SESSION.mount("http://", _adapter)

# 분당 상한은 문서에 없다. 쿠팡·네이버와 같은 보수적 값으로 시작하고,
# 실등록에서 429/차단이 안 나오면 올린다.
_rpm_limiter = RateLimiter(max_per_minute=30, name="elevenst")


# ── 엔드포인트 ────────────────────────────────────────────────
EP_OUTBOUND = "/rest/areaservice/outboundarea"   # GET. 인증 판정용으로 확실히 동작.
EP_INBOUND = "/rest/areaservice/inboundarea"     # GET
EP_CATEGORY = "/rest/cateservice/category"       # GET, ★무인증(키 없이 200)
EP_PRODUCT = "/rest/prodservices/product"        # POST 상품등록
# ⚠️ EP_PRODUCT 는 공식문서 기준값이며 실등록으로 검증되지 않았다.
#    POST 전용이라 GET 프로브로는 확인이 불가능하다(무조건 -997).


# ── 에러코드 (실측) ───────────────────────────────────────────
#   -100  키 누락(빈 키)
#   -200  키에 해당하는 사용자 없음(무효 키). 폐기된 40자 옛 키가 여기 해당.
#   -997  경로·메서드·미개통을 전부 뭉뚱그린 코드. /rest/zzz 도 동일하게 -997 이다.
#          ★이것만 보고 "그 API 가 막혔다" 고 판단하면 안 된다.
#    400  1일 등록 500건 초과
ERR_NO_KEY = "-100"
ERR_BAD_KEY = "-200"
ERR_UNKNOWN_PATH = "-997"
ERR_DAILY_LIMIT = "400"

_ERR_MSG = {
    ERR_NO_KEY: "openapikey 헤더가 비었다 (.env ELEVENST_API_KEY 확인)",
    ERR_BAD_KEY: "키에 해당하는 사용자 없음 — 무효/폐기된 키다. 유효 키는 32자",
    ERR_UNKNOWN_PATH: "경로·메서드·미개통 중 하나 (코드만으로는 구분 불가)",
    ERR_DAILY_LIMIT: "1일 등록 한도 500건 초과",
}


# ── 셀러오피스에서 미리 만들어 둔 값 (2026-08-10 생성) ────────
# 코드에서 만들 수 없고 셀러오피스 화면에서 생성한 뒤 번호만 가져다 쓴다.
ADDR_SEQ_OUT = "1"          # 출고지  — 뉴저지(몰테일)
ADDR_SEQ_IN = "2"           # 반품/교환지 — 동일 주소
OUTSIDE_YN = "Y"            # 해외 출고지
ADDR_LOCATION_OVERSEAS = "02"

# ★템플릿 번호는 **계정 종속**이다 (2026-08-14 셀러오피스 실측으로 확정).
#   계정을 바꾸면 여기도 같이 바꿔야 한다 — 남의 계정 번호를 보내면 등록이 거부된다.
#   11번가에는 템플릿 조회 API 가 없어서 셀러오피스에서 눈으로 읽어야 한다.
TMPLT_BY_ACCT = {
    # 계정      발송예정일    배송정보
    "new": {"send_close": "4296691", "delivery": "4296692"},   # 카리스 글로벌 charisglobal
    # ★아마존매니아(76624081) — 구매대행 전용. 11번가가 68232815 를 폐쇄하고 발급한 계정.
    #   send_close 는 4297913(셀러오피스 화면의 '발송예정일 템플릿')이 **아니다** —
    #   API 가 500 "존재하지 않는 발송마감 템플릿" 을 낸다. 4296691 이 양쪽 계정에서 통한다
    #   (2026-08-15 새 상품으로 실측, prdNo 9556391136).
    # ★4297913 = 셀러오피스 화면의 '발송예정일 템플릿'(2026/08/14 등록·[대표]).
    #   API 는 이 번호도, 스카이포트의 4296691 도 500 으로 거부한다(2026-08-15 계정 명시 실측).
    #   아마존매니아에는 API 가 인정하는 발송마감 템플릿이 아직 없다 — 사람 확인 필요.
    "old": {"send_close": "4297913", "delivery": "4297912"},
}

TMPLT_SEND_CLOSE = "4296691"   # ⚠️구 상수. 계정 분기를 안 탄다 — send_close_tmplt_no() 를 쓸 것
TMPLT_DELIVERY = "4296692"     # ⚠️동상. 현재 전송하는 곳은 없다(배송정보는 필드로 직접 넣는다)


# ★실등록으로 검증된 계정만. 검증 안 된 번호를 보내면 500 으로 등록 자체가 실패한다.
#   new  4296691 — SPEC 346행에서 화면 대조까지 끝냄
#   old  4297913 — 셀러오피스 화면에는 있으나 API 가 "존재하지 않는 발송마감 템플릿" 이라 함
TMPLT_VERIFIED = {"new"}   # ★old 는 미검증 — 어떤 번호도 API 가 받지 않는다


def send_close_tmplt_no(acct: Optional[str] = None) -> str:
    """발송예정일 템플릿 번호. 계정 컨텍스트를 따른다."""
    a = acct or active_account()
    try:
        return TMPLT_BY_ACCT[a]["send_close"]
    except KeyError:
        raise ElevenstError("계정 %r 의 발송예정일 템플릿 번호가 없다 — "
                            "셀러오피스에서 읽어 TMPLT_BY_ACCT 에 넣을 것" % a)


def delivery_tmplt_no(acct: Optional[str] = None) -> str:
    """배송정보 템플릿 번호. 계정 컨텍스트를 따른다."""
    a = acct or active_account()
    try:
        return TMPLT_BY_ACCT[a]["delivery"]
    except KeyError:
        raise ElevenstError("계정 %r 의 배송정보 템플릿 번호가 없다" % a)
# ⚠️ TMPLT_SEND_CLOSE ↔ API 필드 dlvSendCloseTmpltNo 대응은 이름으로 맞춘 것이며
#    실등록으로 검증되지 않았다. 첫 등록 때 반드시 확인할 것.

# 상품정보제공고시 템플릿 — 카테고리 라벨 → 번호.
# 여기 없는 카테고리는 전부 기타재화로 떨어뜨린다(= 등록이 막히지 않는다).
PRD_INFO_TMPLT = {
    "자동차용품": "4296693",
    "주방용품": "4296694",
    "건강기능식품": "4296695",
    "서적": "4296696",
    "스포츠용품": "4296697",
    "가공식품": "4296698",
    "화장품": "4296699",
    "가구": "4296700",
    "침구류/커튼": "4296701",
    "패션잡화": "4296702",
    "가방": "4296703",
    "영상가전": "4296704",
    "가정용전기": "4296705",
    "계절가전": "4296706",
    "사무용기기": "4296707",
    "광학기기": "4296708",
    "구두/신발": "4296709",
    "소형전자": "4296710",
    "휴대용통신기기": "4296711",
    "악기": "4296712",
    "생활화학": "4296721",
}
PRD_INFO_FALLBACK = "4296720"  # 기타재화
# 의류·영유아용품·귀금속/시계 템플릿은 없다 — 해당 카테고리는 소싱하지 않기로 확정(2026-08-10).

# ── 상품정보제공고시 '유형' 코드 (2026-08-13 셀러오피스 팝업에서 실측) ──────────
# ★위의 PRD_INFO_TMPLT 와 다른 체계다. 그건 셀러오피스 템플릿 ID 이고
#   등록 API <ProductNotification><type> 에 넣는 것은 여기 값이다.
#   유형만으로는 부족하고 유형별 '항목' 코드를 전부 채워야 등록이 통과한다.
NOTICE_TYPE_CODE = {
    "의류": "891011", "구두/신발": "891012", "가방": "891013",
    "패션잡화": "891014", "침구류/커튼": "891015", "가구": "891016",
    "영상가전": "891017", "가정용전기": "891018", "계절가전": "891019",
    "사무용기기": "891020", "광학기기": "891021", "소형전자": "891022",
    "휴대용통신기기": "891023", "내비게이션": "891024", "자동차용품": "891025",
    "의료기기": "891026", "주방용품": "891027", "화장품": "891028",
    "귀금속/보석/시계류": "891029", "농수축산물": "891030", "가공식품": "891031",
    "건강기능식품": "891032", "어린이제품": "891033", "악기": "891034",
    "스포츠용품": "891035", "서적": "891036", "호텔/펜션예약": "891037",
    "여행패키지": "891038", "항공권": "891039", "렌터카": "891040",
    "물품대여(정수기등)": "891041", "물품대여(서적등)": "891042",
    "디지털콘텐츠": "891043", "상품권/쿠폰": "891044", "기타재화": "891045",
    "모바일쿠폰": "942188", "영화/공연": "942190", "기타용역": "942191",
    "생활화학제품": "1149547", "살생물제품": "1149546",
}
NOTICE_TYPE_FALLBACK = "891045"   # 기타 재화


def notice_type_code(label):
    """카테고리 라벨 → 고시 유형 코드. 못 찾으면 기타재화."""
    if label:
        hit = NOTICE_TYPE_CODE.get(label.strip())
        if hit:
            return hit
    return NOTICE_TYPE_FALLBACK




# ── 고시 '항목' 코드 (유형별) ────────────────────────────────────────────────
# 값이 None 이면 호출부가 채운다. 문자열이면 그 값을 그대로 쓴다.
# ★유형에 정의된 항목을 하나도 빠짐없이 넣어야 한다 —
#   개수가 모자라면 "고시 항목 개수가 일치하지 않습니다" 로 거부된다.
NOTICE_ITEMS = {
    # 화장품 (2026-08-13 셀러오피스 실측, 11항목)
    "891028": [
        ("23756150", "상품상세설명 참조"),   # 사용기한 또는 개봉 후 사용기간
        ("23756175", "상품상세설명 참조"),   # 사용방법
        ("23756203", "상품상세설명 참조"),   # 사용할 때 주의사항
        ("23756754", "상품상세설명 참조"),   # 소비자상담 관련 전화번호
        ("23757030", "상품상세설명 참조"),   # 기능성화장품 심사 필함 문구
        ("37088489", "상품상세설명 참조"),   # 화장품제조업자·책임판매업자·맞춤형판매업자
        ("23759095", "미국"),                # 제조국 ★구매대행 = 미국
        ("23759521", "상품상세설명 참조"),   # 제품 주요 사양
        ("23759684", "상품상세설명 참조"),   # 화장품법상 기재·표시 모든 성분
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),   # 품질보증기준
        ("23757202", "상품상세설명 참조"),   # 내용물의 용량 또는 중량
    ],
    # 생활화학제품 (11항목)
    "1149547": [
        ("176316020", "상품상세설명 참조"),                                 # 품목 및 제품명
        ("176316074", "상품상세설명 참조"),                                 # 용도(표백제의 경우 계열을 함께 표시) 및 제형
        ("176316124", "상품상세설명 참조"),                                 # 제조연월 및 유통기한 (유통기한의 경우 해당 없는 제품은 생략 가능)
        ("176316199", "상품상세설명 참조"),                                 # 중량ㆍ용량ㆍ매수ㆍ크기
        ("176316337", "상품상세설명 참조"),                                 # 효과·효능 (승인대상 제품에 한함)
        ("23756754", "상품상세설명 참조"),                                 # 소비자상담 관련 전화번호
        ("176316410", "상품상세설명 참조"),                                 # 어린이 보호포장 대상 제품 유무
        ("176316456", "상품상세설명 참조"),                                 # 제품에 사용된 화학물질 명칭
        ("176316505", "상품상세설명 참조"),                                 # 사용상 주의사항
        ("176316535", "상품상세설명 참조"),                                 # 안전기준적합확인신고번호 또는 안전확인대상생활화학제품승인번호
        ("176316383", "미국"),                                        # 수입자(수입제품에 한함), 제조국 및 제조사 
    ],
    # 구두/신발 (8항목)
    "891012": [
        ("11835", "상품상세설명 참조"),                                 # 색상
        ("11905", None),                                        # 제조자/수입자
        ("23759095", "미국"),                                        # 제조국
        ("40748371", "상품상세설명 참조"),                                 # 제품 주소재
        ("23760034", "상품상세설명 참조"),                                 # 치수
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("23759972", "상품상세설명 참조"),                                 # 취급시 주의사항
    ],
    # 가방 (9항목)
    "891013": [
        ("11835", "상품상세설명 참조"),                                 # 색상
        ("11848", "상품상세설명 참조"),                                 # 소재
        ("11905", None),                                        # 제조자/수입자
        ("11908", "상품상세설명 참조"),                                 # 종류
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("23759095", "미국"),                                        # 제조국
        ("23759972", "상품상세설명 참조"),                                 # 취급시 주의사항
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("11932", "상품상세설명 참조"),                                 # 크기,용량,형태
    ],
    # 패션잡화 (8항목)
    "891014": [
        ("11848", "상품상세설명 참조"),                                 # 소재
        ("11905", None),                                        # 제조자/수입자
        ("11908", "상품상세설명 참조"),                                 # 종류
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("23759972", "상품상세설명 참조"),                                 # 취급시 주의사항
        ("23760034", "상품상세설명 참조"),                                 # 치수
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23759095", "미국"),                                        # 제조국
    ],
    # 침구류/커튼 (9항목)
    "891015": [
        ("11835", "상품상세설명 참조"),                                 # 색상
        ("11905", None),                                        # 제조자/수입자
        ("17461", "상품상세설명 참조"),                                 # 제품구성
        ("23756520", "상품상세설명 참조"),                                 # 세탁방법 및 취급시 주의사항
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("23759468", "상품상세설명 참조"),                                 # 제품 소재
        ("23760034", "상품상세설명 참조"),                                 # 치수
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23759095", "미국"),                                        # 제조국
    ],
    # 가구 (12항목)
    "891016": [
        ("11835", "상품상세설명 참조"),                                 # 색상
        ("11905", None),                                        # 제조자/수입자
        ("11932", "상품상세설명 참조"),                                 # 크기,용량,형태
        ("23756017", "상품상세설명 참조"),                                 # 배송·설치비용
        ("23759095", "미국"),                                        # 제조국
        ("469865457", "상품상세설명 참조"),                                 # 재공급(리퍼브) 가구의 경우 재공급 사유 및 하자 부위에 관한 정보
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("23760470", "상품상세설명 참조"),                                 # KC 인증정보   
        ("3125646", "상품상세설명 참조"),                                 # 구성품
        ("37089196", "상품상세설명 참조"),                                 # 품명
        ("23759652", "상품상세설명 참조"),                                 # 주요 소재
    ],
    # 영상가전 (12항목)
    "891017": [
        ("11800", None),                                        # 품명 및 모델명
        ("11905", None),                                        # 제조자/수입자
        ("11932", "상품상세설명 참조"),                                 # 크기,용량,형태
        ("23758072", "상품상세설명 참조"),                                 # KC 인증정보  
        ("23758987", "상품상세설명 참조"),                                 # 정격전압, 소비전력
        ("89784478", "상품상세설명 참조"),                                 # 에너지소비효율등급
        ("23759905", "상품상세설명 참조"),                                 # 추가설치비용
        ("23759938", "상품상세설명 참조"),                                 # 동일모델의 출시년월
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23760396", "상품상세설명 참조"),                                 # 화면사양
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("23759095", "미국"),                                        # 제조국
    ],
    # 가정용전기 (11항목)
    "891018": [
        ("11800", None),                                        # 품명 및 모델명
        ("11905", None),                                        # 제조자/수입자
        ("11932", "상품상세설명 참조"),                                 # 크기,용량,형태
        ("23758072", "상품상세설명 참조"),                                 # KC 인증정보  
        ("23758987", "상품상세설명 참조"),                                 # 정격전압, 소비전력
        ("89784478", "상품상세설명 참조"),                                 # 에너지소비효율등급
        ("23759905", "상품상세설명 참조"),                                 # 추가설치비용
        ("23759938", "상품상세설명 참조"),                                 # 동일모델의 출시년월
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("23759095", "미국"),                                        # 제조국
    ],
    # 계절가전 (12항목)
    "891019": [
        ("11800", None),                                        # 품명 및 모델명
        ("11905", None),                                        # 제조자/수입자
        ("23755843", "상품상세설명 참조"),                                 # 냉난방면적
        ("23758072", "상품상세설명 참조"),                                 # KC 인증정보  
        ("23758987", "상품상세설명 참조"),                                 # 정격전압, 소비전력
        ("89784478", "상품상세설명 참조"),                                 # 에너지소비효율등급
        ("23759905", "상품상세설명 참조"),                                 # 추가설치비용
        ("23759938", "상품상세설명 참조"),                                 # 동일모델의 출시년월
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("469867148", "상품상세설명 참조"),                                 # 크기, 형태 (실외기 포함)
        ("23759095", "미국"),                                        # 제조국
    ],
    # 사무용기기 (11항목)
    "891020": [
        ("11800", None),                                        # 품명 및 모델명
        ("11905", None),                                        # 제조자/수입자
        ("23758987", "상품상세설명 참조"),                                 # 정격전압, 소비전력
        ("23759095", "미국"),                                        # 제조국
        ("23759650", "상품상세설명 참조"),                                 # 주요 사양
        ("89784478", "상품상세설명 참조"),                                 # 에너지소비효율등급
        ("23760172", "상품상세설명 참조"),                                 # 크기, 무게
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("23760478", "상품상세설명 참조"),                                 # KC 인증정보 
        ("23759938", "상품상세설명 참조"),                                 # 동일모델의 출시년월
    ],
    # 광학기기 (9항목)
    "891021": [
        ("11800", None),                                        # 품명 및 모델명
        ("11905", None),                                        # 제조자/수입자
        ("23759095", "미국"),                                        # 제조국
        ("23759650", "상품상세설명 참조"),                                 # 주요 사양
        ("23760478", "상품상세설명 참조"),                                 # KC 인증정보 
        ("23760172", "상품상세설명 참조"),                                 # 크기, 무게
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("23759938", "상품상세설명 참조"),                                 # 동일모델의 출시년월
    ],
    # 소형전자 (10항목)
    "891022": [
        ("11800", None),                                        # 품명 및 모델명
        ("11905", None),                                        # 제조자/수입자
        ("23758385", "상품상세설명 참조"),                                 # 정격전압/소비전력
        ("23759095", "미국"),                                        # 제조국
        ("23760470", "상품상세설명 참조"),                                 # KC 인증정보   
        ("23759938", "상품상세설명 참조"),                                 # 동일모델의 출시년월
        ("23760172", "상품상세설명 참조"),                                 # 크기, 무게
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("23759650", "상품상세설명 참조"),                                 # 주요 사양
    ],
    # 휴대용통신기기 (10항목)
    "891023": [
        ("11800", None),                                        # 품명 및 모델명
        ("11905", None),                                        # 제조자/수입자
        ("23757697", "상품상세설명 참조"),                                 # 이동통신 가입조건
        ("23759095", "미국"),                                        # 제조국
        ("23760478", "상품상세설명 참조"),                                 # KC 인증정보 
        ("23759938", "상품상세설명 참조"),                                 # 동일모델의 출시년월
        ("23760172", "상품상세설명 참조"),                                 # 크기, 무게
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("23759650", "상품상세설명 참조"),                                 # 주요 사양
    ],
    # 자동차용품 (11항목)
    "891025": [
        ("11800", None),                                        # 품명 및 모델명
        ("11905", None),                                        # 제조자/수입자
        ("11932", "상품상세설명 참조"),                                 # 크기,용량,형태
        ("176307481", "상품상세설명 참조"),                                 # 검사합격증 번호
        ("23757857", "상품상세설명 참조"),                                 # KC 인증정보
        ("36743139", "상품상세설명 참조"),                                 # 제품사용으로 인한 위험 및 유의사항
        ("23759938", "상품상세설명 참조"),                                 # 동일모델의 출시년월
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("3674189", "상품상세설명 참조"),                                 # 적용차종
        ("23759095", "미국"),                                        # 제조국
    ],
    # 주방용품 (10항목)
    "891027": [
        ("11800", None),                                        # 품명 및 모델명
        ("11900", "상품상세설명 참조"),                                 # 재질
        ("11905", None),                                        # 제조자/수입자
        ("11932", "상품상세설명 참조"),                                 # 크기,용량,형태
        ("37088317", "상품상세설명 참조"),                                 # 
        ("23759938", "상품상세설명 참조"),                                 # 동일모델의 출시년월
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("3125646", "상품상세설명 참조"),                                 # 구성품
        ("23759095", "미국"),                                        # 제조국
    ],
    # 가공식품 (11항목)
    "891031": [
        ("176312674", "상품상세설명 참조"),                                 # 소비자안전을 위한 주의사항 
        ("176317774", "상품상세설명 참조"),                                 # 제품명 
        ("176398001", "상품상세설명 참조"),                                 # 제조연월일, 소비기한 또는 품질유지기한 
        ("176400445", "미국"),                                        # 생산자 및 소재지 (수입품의 경우 생산자, 수입자 및 제조국)
        ("23756754", "상품상세설명 참조"),                                 # 소비자상담 관련 전화번호
        ("42155152", "상품상세설명 참조"),                                 # 포장단위별 내용물의 용량(중량), 수량
        ("23757095", "상품상세설명 참조"),                                 # 영양성분 (영양성분 표시대상 식품에 한함)
        ("23757245", "미국"),                                        # 원재료명 (`농수산물의 원산지 표시 등에 관한 법률`에 따른 원산지 표시 포함)
        ("23757260", "상품상세설명 참조"),                                 # 유전자변형식품의 경우의 표시
        ("42154823", "상품상세설명 참조"),                                 # 수입식품에 해당하는 경우 “수입식품안전관리특별법에 따른 수입신고를 필함”의 문구
        ("23757000", "상품상세설명 참조"),                                 # 식품의 유형
    ],
    # 건강기능식품 (13항목)
    "891032": [
        ("11906", "상품상세설명 참조"),                                 # 제조업소의 명칭과 소재지 (수입품의 경우 수입업소명, 제조업소명 및 수출국명)
        ("176312674", "상품상세설명 참조"),                                 # 소비자안전을 위한 주의사항 
        ("176317774", "상품상세설명 참조"),                                 # 제품명 
        ("23755783", "상품상세설명 참조"),                                 # 기능정보
        ("23756446", "상품상세설명 참조"),                                 # 섭취량, 섭취방법 및 섭취 시 주의사항 및 부작용 가능성
        ("23756754", "상품상세설명 참조"),                                 # 소비자상담 관련 전화번호
        ("42155152", "상품상세설명 참조"),                                 # 포장단위별 내용물의 용량(중량), 수량
        ("23757103", "상품상세설명 참조"),                                 # 영양정보
        ("23757245", "미국"),                                        # 원료명 및 함량(｢농수산물의 원산지 표시 등에 관한 법률｣에 따른 원산지 표시 
        ("23757304", "상품상세설명 참조"),                                 # 
        ("23759354", "상품상세설명 참조"),                                 # 소비기한 및 보관방법
        ("23759747", "상품상세설명 참조"),                                 # 질병의 예방 및 치료를 위한 의약품이 아니라는 내용의 표현
        ("23756963", "상품상세설명 참조"),                                 # 수입식품에 해당하는 경우 “수입식품안전관리특별법에 따른 수입신고를 필함”의 문구
    ],
    # 악기 (11항목)
    "891034": [
        ("11800", None),                                        # 품명 및 모델명
        ("11835", "상품상세설명 참조"),                                 # 색상
        ("11900", "상품상세설명 참조"),                                 # 재질
        ("11905", None),                                        # 제조자/수입자
        ("11932", "상품상세설명 참조"),                                 # 크기,용량,형태
        ("23760437", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호
        ("23756377", "상품상세설명 참조"),                                 # 상품별 세부 사양
        ("23759095", "미국"),                                        # 제조국
        ("23759938", "상품상세설명 참조"),                                 # 동일모델의 출시년월
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),                       # 품질보증기준
        ("17461", "상품상세설명 참조"),                                 # 제품구성
    ],
    # 서적 (8항목)
    "891036": [
        ("11901", "상품상세설명 참조"),                                 # 저자
        ("11924", "상품상세설명 참조"),                                 # 출판사
        ("11932", "상품상세설명 참조"),                                 # 크기,용량,형태
        ("36743120", "상품상세설명 참조"),                                 # 목차 또는 책소개 (아동용 학습교재의 경우 사용연령을 포함)
        ("23674328", "상품상세설명 참조"),                                 # 도서명
        ("23674410", "상품상세설명 참조"),                                 # 쪽수 (전자책의 경우 제외)
        ("23674466", "상품상세설명 참조"),                                 # 발행일
        ("17461", "상품상세설명 참조"),                                 # 제품구성
    ],
    # 기타재화 (5항목)
    "891045": [
        ("11800", None),                                        # 품명 및 모델명
        ("11905", None),                                        # 제조자/수입자
        ("23760413", "상품상세설명 참조"),                                 # A/S 책임자와 전화번호 또는 소비자상담 관련 전화번호
        ("23759100", "미국"),                                        # 제조국 또는 원산지
        ("23756033", "상품상세설명 참조"),                                 # 법에 의한 인증·허가 등을 받았음을 확인할 수 있는 경우 그에 대한 사항
    ],
    # 스포츠용품 (기존 elevenst_single.py 검증본, 12항목)
    "891035": [
        ("11800", None),                     # ★None → 호출부가 상품명을 넣는다
        ("11835", "상품상세설명 참조"),
        ("11900", "상품상세설명 참조"),
        ("11905", None),                     # ★None → 호출부가 브랜드를 넣는다
        ("17461", "상품상세설명 참조"),
        ("23760454", "상세설명 참조"),
        ("23759095", "미국"),                # 제조국
        ("23759938", "상품상세설명 참조"),
        ("23760223", "상품상세설명 참조"),
        ("23760386", "관련법 및 소비자분쟁해결기준에 따름"),   # 품질보증기준
        ("23760437", "상품상세설명 참조"),
        ("23756377", "상품상세설명 참조"),
    ],
}

# ── 11번가 대분류(depth=1) → 고시 유형 ────────────────────────────────────
# ★여기 없으면 등록을 막는다. 기타재화로 떨어뜨리지 않는다 —
#   화장품을 기타재화 고시로 올리면 표시광고 문제이고, 조용히 통과하는 게 더 위험하다.
#   항목 코드를 확보한 유형만 여기 넣을 것. 새 유형은 셀러오피스 팝업에서 떠서 추가한다.
CTGR_NOTICE_TYPE = {
    # 가전 — 카테고리가 '디지털/가전' 트리면 고시도 가전 계열이다
    "1148762": "891018",   # 주방가전 → 가정용전기 (사람 검증 2026-08-15, 냉장고 정수필터)
    # 화장품 891028
    "1001324": "891028",   # 스킨케어
    "1001325": "891028",   # 메이크업
    "1001326": "891028",   # 선케어
    "1001327": "891028",   # 남성화장품
    "1001328": "891028",   # 클렌징/필링
    "1001329": "891028",   # 헤어케어
    "1001330": "891028",   # 바디케어
    "1001331": "891028",   # 네일케어
    "1001332": "891028",   # 향수
    # ── 해외직구(8286) 트리 ★글로벌 회원은 여기에만 등록 가능하다 ──
    #   depth=1 이 항상 '해외직구' 라 depth=3 → depth=2 순으로 본다.
    #   ★depth=2 는 거칠다: '스포츠/레저' 아래에 자동차용품·운동화·의류가 다 들어있다.
    #     그래서 depth=3 을 먼저 맞춘다.
    #
    #   depth=3 — 스포츠/레저 하위 (항목표 있는 것만)
    "1020701": "891035",   # 골프용품
    "1020702": "891035",   # 골프웨어&잡화
    "10179": "891035",     # 골프클럽
    "10176": "891035",     # 구기용품
    "1020740": "891035",   # 낚시
    "1020726": "891035",   # 등산/아웃도어/캠핑
    "10180": "891035",     # 라켓스포츠
    "10175": "891035",     # 수영/수상스포츠
    "10178": "891035",     # 스노보드/스키
    "123480": "891035",    # 야구
    "10172": "891035",     # 자전거/바이크
    "123474": "891035",    # 축구
    "10174": "891035",     # 피트니스
    "1020744": "891025",   # 자동차용품    (2026-08-13 항목표 확보)
    "10167": "891012",     # 운동화/스니커즈 → 구두/신발 (동)
    # ★스포츠남성패션(957367)·스포츠여성패션(957371)은 의류(891011) 인데
    #   의류 템플릿이 없다 — 의류는 소싱하지 않기로 확정(2026-08-10). 비워 둔다.
    #
    #   depth=2 — 하위 전체가 한 유형인 것만
    "10034": "891028",     # 해외직구 > 뷰티 → 화장품 (하위 전부 화장품)
    # 스포츠용품 891035
    "1001390": "891035",   # 스포츠 잡화
    "1001391": "891035",   # 등산/아웃도어
    "1001392": "891035",   # 골프
    "1001393": "891035",   # 캠핑
    "1001394": "891035",   # 낚시
    "1001396": "891035",   # 헬스
    "1001397": "891035",   # 요가/필라테스
    "1001398": "891035",   # 스키/보드/겨울스포츠
    "1001401": "891035",   # 구기/라켓/스포츠
    "1001404": "891035",   # 수영/수상레저
}


def notice_block(type_code, *, title=None, brand=None):
    """<ProductNotification> 조립. 유형 코드를 모르면 (None, 사유) 를 준다.

    ★값 None 인 항목은 여기서 채운다 — 11800=상품명, 11905=브랜드.
      그 외 None 은 '상품상세설명 참조' 로 떨어진다.
    """
    items = NOTICE_ITEMS.get(str(type_code or ""))
    if not items:
        # ★사장 지시 2026-08-14 — 고시 때문에 등록이 막히면 안 된다.
        #   항목표를 못 구한 유형(의류·어린이제품·의료기기 등 18종)은 기타재화로 떨어뜨린다.
        #   기타재화 5항목은 전부 '상품상세설명 참조'(+제조국 미국)라 어느 상품에나 맞는다.
        logger.info("[11st] 고시유형 %s 항목표 없음 → 기타재화(%s) 폴백",
                    type_code, NOTICE_TYPE_FALLBACK)
        type_code = NOTICE_TYPE_FALLBACK
        items = NOTICE_ITEMS.get(NOTICE_TYPE_FALLBACK)
        if not items:
            return None, "기타재화 항목표마저 없다 — NOTICE_ITEMS 확인 필요"
    parts = []
    for code, val in items:
        if val is None:
            if code == "11800":
                val = (title or "")[:50]
            elif code == "11905":
                val = brand or "상품상세설명 참조"
            else:
                val = "상품상세설명 참조"
        parts.append("<item><code>%s</code><name><![CDATA[%s]]></name></item>" % (code, val))
    return ("<ProductNotification><type>%s</type>%s</ProductNotification>"
            % (type_code, "".join(parts))), None


# ── 한도 ──────────────────────────────────────────────────────
DAILY_LIMIT = 500      # API 문서. 초과 시 resultCode 400.
TOTAL_LIMIT = 5000     # 셀러오피스 5등급. 1~4등급이면 10,000.
# ⚠️ TOTAL_LIMIT 은 "매월 1일 부여" 문구가 한도 재설정인지 월간 쿼터인지 불명확하다.
#    동시 리스팅 상한으로 보고 짰다. 실등록에서 확인할 것.


class ElevenstError(RuntimeError):
    """11번가 API 실패. code 는 resultCode 문자열(없으면 None)."""

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


# ── 계정 라우팅 ───────────────────────────────────────────────
# 컨텍스트 미설정 시 ELEVENST_ACTIVE(.env, 기본 new). `with elevenst_account("old"):`
# 블록 안에서만 구계정 자격증명으로 호출한다 — 한 프로세스가 두 계정을 모두 다룬다.
_ACCOUNT_CTX = contextvars.ContextVar("elevenst_account", default=None)


@contextmanager
def elevenst_account(account: str):
    """이 블록 안의 모든 11번가 호출을 지정 계정('old'|'new')으로 라우팅."""
    token = _ACCOUNT_CTX.set(account)
    try:
        yield
    finally:
        _ACCOUNT_CTX.reset(token)


def active_account() -> str:
    """현재 컨텍스트의 활성 계정. 미설정 시 .env ELEVENST_ACTIVE."""
    return _ACCOUNT_CTX.get() or ELEVENST_ACTIVE


# ── 자격증명 ──────────────────────────────────────────────────
def _key() -> str:
    """현재 계정의 openapikey. 비어 있으면 폴백 없이 죽는다."""
    acct = active_account()
    key = (elevenst_cfg("API_KEY", acct) or "").strip()
    env_name = "ELEVENST_OLD_API_KEY" if acct == "old" else "ELEVENST_API_KEY"
    if not key:
        raise ElevenstError(f"{env_name} 가 .env 에 없다 (계정={acct})")
    if len(key) != 32:
        # 40자짜리 폐기 키가 로컬 .env 사본에 남아 있어 실제로 한 번 물렸다.
        # 형식으로 거를 수 있는 사고라 미리 막는다.
        raise ElevenstError(f"{env_name} 길이가 {len(key)} — 유효 키는 32자다")
    return key


def _mem_no() -> str:
    """현재 계정의 memNo 기대값 — 계정 검증 앵커."""
    acct = active_account()
    return (elevenst_cfg("MEM_NO", acct)
            or ("68232815" if acct == "old" else "76614773"))


# ── 전송 ──────────────────────────────────────────────────────
def _headers() -> dict:
    return {"openapikey": _key(), "Accept": "application/xml"}


def _check_fault(root: ET.Element) -> None:
    """AuthMessage/resultCode 가 실려오면 실패로 간주하고 올린다.

    11번가는 실패해도 HTTP 200 을 준다. 상태코드로 성공을 판정하면 안 된다.
    """
    code = _text(root, "resultCode")
    if code is None:
        return
    if code in ("0", "100", "200"):   # 성공계열로 확인된 값
        return
    # ★실패 응답은 <ClientMessage><message>…</message> 다. resultMessage 만 보면
    #   사유가 통째로 안 보이고 "resultCode=500" 만 남는다(2026-08-13 실측).
    msg = _text(root, "resultMessage") or _text(root, "message") or ""
    hint = _ERR_MSG.get(code, "")
    raise ElevenstError(
        f"11번가 오류 resultCode={code} {msg}" + (f" — {hint}" if hint else ""),
        code=code,
    )


def cut_bytes(text: str, limit: int, enc: str = ENC) -> str:
    """EUC-KR 바이트 기준으로 자른다.

    ★11번가 상품명은 **100Byte** 다. 공식 필드표의 "100자"는 틀렸다 —
      서버가 "상품명은 100Byte 이하로 등록이 가능합니다" 로 답한다(2026-08-13 실측).
      한글은 EUC-KR 2바이트라 실질 50자다.
    """
    b = (text or "").encode(enc, errors="replace")
    if len(b) <= limit:
        return text or ""
    out = b[:limit]
    cut = ""
    while out:
        try:
            cut = out.decode(enc)
            break
        except UnicodeDecodeError:
            out = out[:-1]      # 멀티바이트 문자 중간에서 잘렸다
    if not cut:
        return ""
    # ★단어 중간에서 잘렸으면 그 토막을 버린다 — "차량용 루프백 랙 유" 같은 꼬리를 막는다
    if len(cut) < len(text) and not text[len(cut)].isspace() and " " in cut.rstrip():
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" -,·/|~")


def _text(root: ET.Element, tag: str) -> Optional[str]:
    """네임스페이스 무시하고 첫 태그의 텍스트. 응답이 ns2: 프리픽스를 달고 온다."""
    el = root.find(f".//{{*}}{tag}")
    if el is None:
        el = root.find(f".//{tag}")
    return el.text if el is not None else None


_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.S)


def _parse(raw: bytes) -> ET.Element:
    """EUC-KR 바이트 → ElementTree.

    ★bytes 를 그대로 넘기면 안 된다. 파이썬 expat 은 멀티바이트 인코딩을 모르고
    `ValueError: multi-byte encodings are not supported` 로 죽는다. 파이썬에서 먼저
    디코딩한 뒤, str 에 남은 `<?xml encoding="euc-kr"?>` 선언은 이번엔
    "encoding declaration in unicode string" 으로 걸리므로 같이 떼어낸다.
    """
    text = raw.decode(ENC, errors="replace")
    text = _DECL_RE.sub("", text, count=1).lstrip()
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise ElevenstError(f"XML 파싱 실패: {e} / 응답머리: {text[:200]}") from e


def _request(method: str, path: str, *, body: Optional[str] = None,
             timeout: int = 20, authed: bool = True) -> ET.Element:
    _rpm_limiter.wait()
    url = BASE + path
    headers = _headers() if authed else {}
    data = body.encode(ENC, errors="replace") if body else None
    if data is not None:
        # ★text/xml 이어야 한다. application/xml 을 보내면 11번가가 415 를 주는데
        #   본문이 비어 있어서 파서가 "no element found" 로 죽는다 — 원인이 안 보인다.
        #   (2026-08-11 실등록 검증에서 확인)
        headers["Content-Type"] = f"text/xml; charset={ENC}"

    try:
        resp = _SESSION.request(method, url, headers=headers, data=data, timeout=timeout)
    except requests.RequestException as e:
        raise ElevenstError(f"11번가 요청 실패 {method} {path}: {e}") from e

    root = _parse(resp.content)
    _check_fault(root)
    return root


# ── 계정 검증 ─────────────────────────────────────────────────
# ★계정별로 따로 캐시한다. 전역 플래그 하나면 신계정에서 통과한 검증이 구계정
#   호출까지 통과시켜, 정작 계정이 바뀐 순간을 못 잡는다.
_verified: set[str] = set()
_verify_lock = threading.Lock()


def verify_account(force: bool = False) -> str:
    """이 키가 의도한 계정 것인지 확인하고 memNo 를 돌려준다.

    11번가에는 "이 키가 누구 것인가" 를 알려주는 API 가 없다. 로그인 ID 는 어디에도
    안 나오고, 주소록이 돌려주는 memNo 가 유일한 앵커다. 기대값과 다르면 즉시 죽인다.
    ★등록 배치 시작 시 1회 호출할 것 — 안 하면 엉뚱한 계정에 상품이 올라가도 모른다.
    """
    acct = active_account()
    expect = _mem_no()
    if acct in _verified and not force:
        return expect

    with _verify_lock:
        root = _request("GET", EP_OUTBOUND)
        mem_no = _text(root, "memNo")
        if not mem_no:
            raise ElevenstError("주소록 응답에 memNo 가 없다 — 계정 확인 불가")
        if mem_no.strip() != str(expect).strip():
            raise ElevenstError(
                f"계정 불일치: 키가 가리키는 memNo={mem_no}, 기대값={expect} (계정={acct}). "
                "다른 계정 키가 .env 에 들어갔을 수 있다"
            )
        _verified.add(acct)
        logger.info("[11st] 계정 확인 acct=%s memNo=%s", acct, mem_no)
        return mem_no


# ── 주소록 ────────────────────────────────────────────────────
def _addresses(path: str) -> list[dict]:
    root = _request("GET", path)
    out = []
    for node in root.findall(".//{*}inOutAddress"):
        out.append({
            "addr_seq": _text(node, "addrSeq"),
            "addr_nm": _text(node, "addrNm"),
            "addr": _text(node, "addr"),
            "rcvr_nm": _text(node, "rcvrNm"),
            "tel": _text(node, "gnrlTlphnNo"),
            "mem_no": _text(node, "memNo"),
        })
    return out


def outbound_areas() -> list[dict]:
    """출고지 목록. addrSeq=1 이 뉴저지(몰테일)."""
    return _addresses(EP_OUTBOUND)


def inbound_areas() -> list[dict]:
    """반품/교환지 목록. addrSeq=2 가 동일 주소."""
    return _addresses(EP_INBOUND)


# ── 카테고리 (무인증) ─────────────────────────────────────────
def category_tree() -> ET.Element:
    """전체 카테고리 트리. ★키 없이 열린다. 8.8MB 라 호출 비용이 크다 —
    적재는 별도 스크립트로 1회만 돌리고 이 함수를 루프에서 부르지 말 것."""
    return _request("GET", EP_CATEGORY, timeout=120, authed=False)


def prd_info_tmplt_no(category_label: Optional[str]) -> str:
    """카테고리 라벨 → 상품정보제공고시 템플릿 번호. 못 찾으면 기타재화."""
    if category_label:
        hit = PRD_INFO_TMPLT.get(category_label.strip())
        if hit:
            return hit
    return PRD_INFO_FALLBACK


# ── 한도 ──────────────────────────────────────────────────────
def quota() -> dict:
    """오늘 등록분 / 전체 등록분. listings_pa 는 channel 선두 인덱스가 있고
    elevenst 행은 TOTAL_LIMIT(5,000) 로 묶여 있어 이 집계는 항상 가볍다."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE WHEN date(created_at) = date('now','localtime')
                          THEN 1 ELSE 0 END) AS today
               FROM listings_pa
               WHERE channel = ?
                 AND channel_product_id IS NOT NULL
                 AND channel_product_id != ''""",
            (CHANNEL,),
        ).fetchone()
    total = row["total"] or 0
    today = row["today"] or 0
    return {
        "today": today,
        "total": total,
        "daily_remaining": max(0, DAILY_LIMIT - today),
        "total_remaining": max(0, TOTAL_LIMIT - total),
    }


def assert_quota() -> None:
    """등록 직전 게이트. 한도를 넘겨 호출하면 API 가 어차피 400 을 주지만,
    그 전에 죽여서 쓸데없는 페이로드 생성·호출을 막는다."""
    q = quota()
    if q["daily_remaining"] <= 0:
        raise ElevenstError(f"1일 등록 한도 소진 ({q['today']}/{DAILY_LIMIT})", code=ERR_DAILY_LIMIT)
    if q["total_remaining"] <= 0:
        raise ElevenstError(f"전체 등록 한도 소진 ({q['total']}/{TOTAL_LIMIT})")


# ── 상품 등록 ─────────────────────────────────────────────────
def xml_escape(value) -> str:
    """페이로드에 넣기 전 이스케이프. 상품명·브랜드에 & 가 흔하다."""
    return _xml_escape("" if value is None else str(value))


def cdata(html: str) -> str:
    """htmlDetail 은 CDATA 필수. 내부에 ]]> 가 있으면 전문이 깨진다."""
    return "<![CDATA[" + (html or "").replace("]]>", "]]&gt;") + "]]>"


def register_product(payload_xml: str, *, verify: bool = True) -> dict:
    """상품 등록. payload_xml 은 elevenst_lister 가 만든 <Product> 전문.

    반환: {"product_no": str|None, "result_code": str|None, "message": str|None}
    """
    if verify:
        verify_account()
    assert_quota()

    body = payload_xml if payload_xml.lstrip().startswith("<?xml") else (
        f'<?xml version="1.0" encoding="{ENC}"?>\n{payload_xml}'
    )
    root = _request("POST", EP_PRODUCT, body=body, timeout=60)
    # ★상품번호는 엘리먼트가 아니라 속성으로 오는 경우가 있다(옥션에서도 같은 함정을 겪었다).
    #   못 읽으면 등록됐는데 못 내리는 상태가 되므로 엘리먼트 → 속성 → 원문 순으로 뒤진다.
    no = _text(root, "prdNo") or _text(root, "productNo")
    if not no:
        for el in root.iter():
            for k, v in el.attrib.items():
                if k.lower() in ("prdno", "productno") and str(v).strip():
                    no = str(v).strip()
                    break
            if no:
                break
    raw = ET.tostring(root, encoding="unicode")
    if not no:
        import re as _re
        m = _re.search(r"\b(\d{9,12})\b", raw)   # 11번가 상품번호는 10자리 안팎이다
        if m:
            no = m.group(1)
    return {
        "product_no": no,
        "result_code": _text(root, "resultCode"),
        "message": _text(root, "resultMessage") or _text(root, "message"),
        "raw": raw,          # ★성공/실패 불문 원문을 남긴다. 없으면 원인 추적이 불가능하다
    }

# ── 상품 조회 3종 (2026-08-15 공식문서) ──────────────────────────
#   ★셋 다 **옵션 목록을 안 준다.** optionAllAddPrc/optionAllQty 는 조합형 일괄값이고
#     개별 옵션·옵션ID 는 응답에 없다. 우리는 싱글옵션으로 등록하는데 그 블록이 안 온다.
#     → 옵션ID 회수는 주문 수신 경로에서 해야 한다. 여기서 '옵션 없음'으로 단정하면 안 된다.
SEL_STAT = {
    "101": "승인대기", "102": "승인전", "103": "판매중", "104": "품절",
    "105": "전시중지", "106": "판매정상종료", "107": "판매강제종료", "108": "판매금지",
}
_LIVE_STAT = ("101", "102", "103", "104")      # 아직 채널에 살아 있는 상태


def _prod_dict(el: ET.Element) -> dict:
    """<Product> 또는 <ns2:product> 한 덩어리 → dict. 네임스페이스는 무시한다."""
    out = {}
    for ch in el:
        tag = ch.tag.split("}")[-1]
        out[tag] = (ch.text or "").strip()
    out["selStatNm"] = out.get("selStatNm") or SEL_STAT.get(out.get("selStatCd", ""), "")
    out["alive"] = out.get("selStatCd") in _LIVE_STAT
    return out


def product_by_no(prd_no: str) -> Optional[dict]:
    """신규상품조회 — 상품번호로 단건. 없으면 None."""
    try:
        root = _request("GET", "/rest/prodmarketservice/prodmarket/%s" % prd_no)
    except ElevenstError as e:
        if "조회" in str(e) or "없" in str(e):
            return None
        raise
    if root.tag.split("}")[-1].lower() != "product":
        el = root.find(".//{*}product") or root.find(".//Product")
        if el is None:
            return None
        root = el
    d = _prod_dict(root)
    return d if d.get("prdNo") else None


def product_by_seller_code(code: str) -> list:
    """셀러상품조회 — 판매자상품코드(=우리는 ASIN)로 조회.

    ★prdNo 를 몰라도 되는 유일한 경로다. 우리 DB 에 기록이 없는 채널 상품을 잡는다.
      같은 코드로 여러 건이 올라가 있을 수 있어 목록으로 돌려준다(중복 등록 탐지).
    """
    try:
        root = _request("GET", "/rest/prodmarketservice/sellerprodcode/%s" % code)
    except ElevenstError as e:
        if "조회" in str(e) or "없" in str(e):
            return []
        raise
    return [_prod_dict(el) for el in root.findall(".//{*}product")] or \
           [_prod_dict(el) for el in root.findall(".//product")]


def search_products(*, limit: int = 100, sel_stat: Optional[str] = None,
                    start: Optional[int] = None, end: Optional[int] = None) -> list:
    """다중상품조회 — 목록. ★공식문서 경고: limit 는 가능한 작게(최대 500)."""
    body = ["<?xml version=\"1.0\" encoding=\"euc-kr\"?><SearchProduct>"]
    body.append("<limit>%d</limit>" % int(limit))
    if sel_stat:
        body.append("<selStatCd>%s</selStatCd>" % sel_stat)
    if start is not None:
        body.append("<start>%d</start>" % int(start))
    if end is not None:
        body.append("<end>%d</end>" % int(end))
    body.append("</SearchProduct>")
    root = _request("POST", "/rest/prodmarketservice/prodmarket", body="".join(body))
    return [_prod_dict(el) for el in root.findall(".//{*}product")]


# ── 주문 조회 (2026-08-15) ───────────────────────────────────
#   ★주문이 자식 ASIN 을 직접 준다(sellerStockCd). 옵션ID 사전 회수가 필요 없다 —
#     상품조회 API 에 옵션이 없어 막혔던 길이 여기서 열린다.
_ORD_FIELDS = {
    "ordNo": "channel_order_id",         # 주문번호
    "ordPrdSeq": "order_line_seq",       # 주문상품 순번 — 한 주문에 여러 줄
    "prdNo": "channel_product_id",       # 11번가 상품번호
    "sellerPrdCd": "parent_asin",        # ★우리가 넣은 부모 ASIN
    "sellerStockCd": "child_asin",       # ★우리가 넣은 자식 ASIN — 역추적의 핵심
    "prdStckNo": "channel_option_id",    # 옵션ID
    "slctPrdOptNm": "option_name",       # 옵션명
    "prdNm": "product_name",
    "ordQty": "quantity",
    "ordPrdPayAmt": "paid_krw",
    "ordDt": "ordered_at",
    "ordPrdStat": "channel_status",
    "rcvrNm": "receiver_name",
    "rcvrPrtblNo": "receiver_phone",
    "rcvrMailNo": "receiver_zipcode",
    "rcvrBaseAddr": "receiver_addr",
    "rcvrDtlsAddr": "receiver_addr_detail",
    "dlvMsg": "shipping_message",
    "ordPrdCd": "customs_code",          # 개인통관고유부호(있으면)
}


def _order_dict(el: ET.Element) -> dict:
    raw = {}
    for ch in el.iter():
        tag = ch.tag.split("}")[-1]
        if tag != el.tag.split("}")[-1]:
            raw[tag] = (ch.text or "").strip()
    out = {v: raw.get(k) for k, v in _ORD_FIELDS.items()}
    out["_raw"] = raw          # ★첫 주문 때 필드명을 대조하기 위해 원문을 남긴다
    return out


def _orders(path: str) -> list:
    root = _request("GET", path, timeout=30)
    return [_order_dict(el) for el in root.findall(".//{*}order")] or \
           [_order_dict(el) for el in root.findall(".//order")]


def orders_paid(start_yyyymmdd: str, end_yyyymmdd: str) -> list:
    """결제완료(발주 확인 대기). 폴링은 이걸 쓴다.

    ★색인 110/1876 '기간별 결제완료 목록조회'. 문서에 경로가 없어 실호출로 확인했다.
    """
    return _orders("/rest/ordservices/complete/%s/%s" % (start_yyyymmdd, end_yyyymmdd))


def orders_confirmed(start_yyyymmdd: str, end_yyyymmdd: str) -> list:
    """구매확정(판매완료). 정산 대조용."""
    return _orders("/rest/ordservices/completed/%s/%s" % (start_yyyymmdd, end_yyyymmdd))


def order_by_no(ord_no: str) -> list:
    """주문번호별 상태조회. 단건 확인용."""
    return _orders("/rest/ordservices/complete/%s" % ord_no)
