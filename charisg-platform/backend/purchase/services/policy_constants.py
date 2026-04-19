"""
policy_constants.py — 채널 등록 정책 상수 (스마트스토어 + 쿠팡 공유).

스마트스토어 페이로드에 하드코딩되어 있던 값들을 한 곳에 모음.
쿠팡 lister가 동일 값을 쓰도록 import해서 두 채널의 정책을 일치시킨다.
"""

# ── A/S 안내 ────────────────────────────
AS_PHONE = "010-8558-7277"
AS_GUIDE = (
    "해외 구매대행 상품으로 국내 A/S가 불가합니다. "
    "네이버 톡톡 또는 1:1 문의를 이용해주세요."
)

# ── 원산지 / 수입사 ─────────────────────
IMPORTER = "Charis G"
ORIGIN_AREA_CODE_NAVER = "0204000"   # 네이버 originAreaCode (미국)
ORIGIN_COUNTRY_CODE_COUPANG = "US"   # 쿠팡 countryCode

# ── 배송 ─────────────────────────────────
DELIVERY_COMPANY_NAVER = "CJGLS"     # 네이버 deliveryCompany
DELIVERY_COMPANY_COUPANG = "HANJIN"  # 쿠팡 deliveryCompanyCode (실제 운영 페이로드 캡처 확인)
DELIVERY_FEE_TYPE = "FREE"

# ── 반품 / 교환 ──────────────────────────
# 네이버는 smartstore_lister.py에 5000 하드코딩. 쿠팡 해외구매대행은 15000 (실제 캡처).
NAVER_RETURN_FEE = 5000
NAVER_EXCHANGE_FEE = 5000
COUPANG_RETURN_FEE = 15000
COUPANG_EXCHANGE_FEE = 15000
RETURN_CHARGE_NAME = "US return"
RETURN_CONTACT_NUMBER = "2015683865"  # 실제 쿠팡 등록 연락처 (US 현지번호)

# ── 주문/판매 정책 ──────────────────────
DEFAULT_STOCK = 100                  # 기본 재고 수량
MAX_PRODUCT_NAME_LEN = 50            # 네이버/쿠팡 공통 권장 (네이버 100자 한도 내, 50자 권장)
