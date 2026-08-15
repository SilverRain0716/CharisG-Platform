"""PA 전용 AI 처리 파이프라인 — 번역 + SEO + 카테고리 + 상세페이지 HTML 생성."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from backend.purchase.database import get_db
from backend.purchase.services import clean_policy
from backend.purchase.services.image_downloader import download_product_images, fetch_amazon_images
from backend.purchase.services.category_rag import resolve_category
from backend_shared.ai import translate_text, generate_seo

logger = logging.getLogger(__name__)

# ── PA 전용 상세페이지 섹션 (인라인 스타일) ─────────────

PA_SECTION_AUTH = """<div style="background:linear-gradient(135deg,#1B3A5C 0%,#0F2640 100%);padding:44px 40px 40px;text-align:center">
  <div style="margin-bottom:26px">
    <div style="font-size:38px;font-weight:800;color:#fff;letter-spacing:2px">Charis G</div>
    <div style="font-size:15px;color:rgba(255,255,255,0.5);letter-spacing:4px;margin-top:-2px">GLOBAL SOURCING</div>
  </div>
  <div style="width:76px;height:76px;margin:0 auto 20px;background:linear-gradient(145deg,#F5D77A,#D4A843,#F5D77A);border-radius:50%;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 20px rgba(212,168,67,0.4)">
    <span style="font-size:17px;font-weight:800;color:#5C3D0E;text-align:center;line-height:1.3">정품<br>인증</span>
  </div>
  <div style="font-size:40px;font-weight:800;color:#fff;margin-bottom:12px">본 제품은 <span style="color:#E8845A">100% 정품</span>입니다.</div>
  <p style="font-size:20px;color:rgba(255,255,255,0.6);line-height:1.6;margin:0">공식 유통 제품만 취급하며, 가품은 판매하지 않습니다.</p>
</div>
<div style="background:#F7F5F0;padding:36px 40px 40px;text-align:center">
  <div style="font-size:17px;color:#E8845A;font-weight:700;letter-spacing:3px;margin-bottom:24px">WHY CHARIS G</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;max-width:660px;margin:0 auto">
    <div style="background:#fff;border-radius:14px;padding:22px 16px;box-shadow:0 2px 10px rgba(0,0,0,0.05)">
      <span style="font-size:40px;display:block;margin-bottom:10px">\U0001F3E2</span>
      <div style="font-size:21px;font-weight:700;color:#1B3A5C">검증된 글로벌 소싱</div>
    </div>
    <div style="background:#fff;border-radius:14px;padding:22px 16px;box-shadow:0 2px 10px rgba(0,0,0,0.05)">
      <span style="font-size:40px;display:block;margin-bottom:10px">\U0001F4AC</span>
      <div style="font-size:21px;font-weight:700;color:#1B3A5C">신속한 고객 응대</div>
    </div>
    <div style="background:#fff;border-radius:14px;padding:22px 16px;box-shadow:0 2px 10px rgba(0,0,0,0.05)">
      <span style="font-size:40px;display:block;margin-bottom:10px">\U0001F4E6</span>
      <div style="font-size:21px;font-weight:700;color:#1B3A5C">꼼꼼한 검수 포장</div>
    </div>
    <div style="background:#fff;border-radius:14px;padding:22px 16px;box-shadow:0 2px 10px rgba(0,0,0,0.05)">
      <span style="font-size:40px;display:block;margin-bottom:10px">\U0001F69A</span>
      <div style="font-size:21px;font-weight:700;color:#1B3A5C">안전한 택배 배송</div>
    </div>
  </div>
</div>"""

PA_SECTION_SHIPPING = """<div style="background:#E8845A;padding:50px 40px 30px;text-align:center">
  <div style="font-size:31px;font-weight:800;color:#fff;letter-spacing:1px;margin-bottom:20px">Charis G</div>
  <div style="display:inline-block;background:rgba(255,255,255,0.2);border:2px solid rgba(255,255,255,0.4);border-radius:30px;padding:10px 32px;font-size:26px;font-weight:700;color:#fff;margin-bottom:16px">해외배송 절차안내</div>
  <div style="font-size:72px;margin-bottom:8px">🌍</div>
</div>
<div style="background:#fff;padding:40px 36px;text-align:center">
  <div style="display:inline-flex;align-items:center;gap:8px;background:#F0FAF5;border:1px solid #B8E6D0;border-radius:30px;padding:10px 24px;font-size:20px;color:#2D8B5E;font-weight:600;margin-bottom:24px">
    ✈️ <span>본 상품은 <b>해외배송</b> 상품입니다.</span>
  </div>
  <div style="font-size:29px;color:#333;margin-bottom:6px">배송기간은 주문일로부터 약 <span style="font-weight:800;color:#E8845A;font-size:34px">7~20일</span> 정도입니다.</div>
  <div style="font-size:17px;color:#999;margin-bottom:36px;line-height:1.6">(평일 영업일 기준이며 현지 사정 및 공휴일에 따라<br>배송 기간에 차이가 있을 수 있습니다)</div>
  <div style="display:flex;justify-content:center;align-items:flex-start;margin-bottom:32px">
    <div style="display:flex;flex-direction:column;align-items:center;width:110px">
      <div style="width:70px;height:70px;background:#1B3A5C;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:38px;margin-bottom:12px">📋</div>
      <div style="font-size:17px;font-weight:600;color:#333;text-align:center;line-height:1.4">주문 및<br>결제확인</div>
    </div>
    <div style="display:flex;align-items:center;padding-top:24px;color:#1B3A5C;font-size:24px;font-weight:700">→</div>
    <div style="display:flex;flex-direction:column;align-items:center;width:110px">
      <div style="width:70px;height:70px;background:#1B3A5C;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:38px;margin-bottom:12px">🛒</div>
      <div style="font-size:17px;font-weight:600;color:#333;text-align:center;line-height:1.4">현지 재고<br>확인 및 구매</div>
    </div>
    <div style="display:flex;align-items:center;padding-top:24px;color:#1B3A5C;font-size:24px;font-weight:700">→</div>
    <div style="display:flex;flex-direction:column;align-items:center;width:110px">
      <div style="width:70px;height:70px;background:#1B3A5C;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:38px;margin-bottom:12px">🔍</div>
      <div style="font-size:17px;font-weight:600;color:#333;text-align:center;line-height:1.4">물류센터<br>입고 후 검품</div>
    </div>
    <div style="display:flex;align-items:center;padding-top:24px;color:#1B3A5C;font-size:24px;font-weight:700">→</div>
    <div style="display:flex;flex-direction:column;align-items:center;width:110px">
      <div style="width:70px;height:70px;background:#1B3A5C;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:38px;margin-bottom:12px">✈️</div>
      <div style="font-size:17px;font-weight:600;color:#333;text-align:center;line-height:1.4">국제 배송</div>
    </div>
  </div>
  <div style="display:flex;justify-content:center;align-items:flex-start;margin-bottom:10px">
    <div style="display:flex;flex-direction:column;align-items:center;width:110px">
      <div style="width:70px;height:70px;background:#2D8B5E;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:38px;margin-bottom:12px">🛃</div>
      <div style="font-size:17px;font-weight:600;color:#333;text-align:center;line-height:1.4">국내 도착<br>세관/통관</div>
    </div>
    <div style="display:flex;align-items:center;padding-top:24px;color:#1B3A5C;font-size:24px;font-weight:700">→</div>
    <div style="display:flex;flex-direction:column;align-items:center;width:110px">
      <div style="width:70px;height:70px;background:#2D8B5E;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:38px;margin-bottom:12px">🚛</div>
      <div style="font-size:17px;font-weight:600;color:#333;text-align:center;line-height:1.4">통관완료 후<br>국내 배송</div>
    </div>
    <div style="display:flex;align-items:center;padding-top:24px;color:#1B3A5C;font-size:24px;font-weight:700">→</div>
    <div style="display:flex;flex-direction:column;align-items:center;width:110px">
      <div style="width:70px;height:70px;background:#2D8B5E;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:38px;margin-bottom:12px">📬</div>
      <div style="font-size:17px;font-weight:600;color:#333;text-align:center;line-height:1.4">배송 완료</div>
    </div>
  </div>
</div>"""

# 상품 이미지 — {{product_images}} 가 <img> 태그 나열로 치환됨
PA_SECTION_GALLERY = """<div style="background:#fff">{{product_images}}</div>"""

PA_SECTION_NOTICE = """<div style="height:5px;background:linear-gradient(90deg,#E8845A,#1B3A5C)"></div>
<div style="background:#1B3A5C;padding:50px 40px 30px;text-align:center">
  <div style="font-size:30px;font-weight:800;color:#fff;letter-spacing:1px;margin-bottom:14px">Charis G</div>
  <div style="display:inline-block;background:#E8845A;border-radius:30px;padding:10px 28px;font-size:20px;font-weight:700;color:#fff;margin-bottom:12px">해외 구매대행 상품</div>
  <div style="font-size:41px;font-weight:800;color:#fff;margin-bottom:30px">구매 전 꼭 확인해 주세요</div>
</div>
<div style="background:#15304D;padding:0 40px 16px">

  <div style="background:linear-gradient(135deg,#254B73 0%,#1B3A5C 100%);border-radius:16px;margin-bottom:16px;padding:24px 28px">
    <div style="font-size:15px;color:#8FB0CC;font-weight:700;letter-spacing:2px;margin-bottom:8px">CUSTOMS INFORMATION \u00b7 관세 안내</div>
    <div style="font-size:23px;color:#fff;font-weight:700;line-height:1.4">구매금액 <span style="color:#F5B841;font-weight:800">미화 150달러 초과</span> 시 관부가세가 별도로 발생합니다.</div>
    <div style="font-size:17px;color:#B8CDE0;line-height:1.5;margin-top:8px">같은 날 통관되는 주문은 합산되어 과세되니, 여러 건 주문 시 날짜를 나눠 주세요.</div>
    <div style="font-size:17px;color:#B8CDE0;line-height:1.5;margin-top:4px">사업 목적으로 사용하시는 경우에도 관부가세가 부과되며, 미납 시 통관 지연\u00b7폐기될 수 있습니다.</div>
  </div>

  <div style="background:#fff;border-radius:16px;margin-bottom:16px;overflow:hidden">
    <div style="background:#F7F5F0;padding:18px 28px;font-size:24px;font-weight:700;color:#1B3A5C;border-bottom:2px solid #E8845A">구매대행 서비스 안내</div>
    <div style="padding:24px 28px;font-size:19px;color:#555;line-height:1.8">
      \u00b7 당사는 해외 판매처와 고객님을 <span style="color:#1B3A5C;font-weight:600">연결하는 구매대행 서비스</span>이며, 최종 상품\u00b7브랜드 선택은 고객님의 판단에 따릅니다.<br>
      \u00b7 재고를 직접 보유하지 않아 현지 사정에 따라 <span style="color:#D94040;font-weight:600">품절 또는 배송 지연</span>이 발생할 수 있습니다.<br>
      \u00b7 상품 정보는 해외 공식 판매처 자료를 바탕으로 제공하며, 실제 상품과 차이가 있을 수 있습니다.
    </div>
  </div>

  <div style="background:#fff;border-radius:16px;margin-bottom:16px;overflow:hidden">
    <div style="background:#F7F5F0;padding:18px 28px;font-size:24px;font-weight:700;color:#1B3A5C;border-bottom:2px solid #E8845A">통관 정보 확인</div>
    <div style="padding:24px 28px">
      <div style="font-size:21px;font-weight:700;color:#1B3A5C;margin-bottom:14px">\U0001F4CB 아래 3가지가 반드시 일치해야 합니다</div>
      <div style="background:#FFF4F0;border:1px solid #F5D9CB;border-radius:12px;padding:20px;text-align:center;font-size:21px;font-weight:700;color:#D94040;line-height:1.6;margin-bottom:16px">
        수령인 성함 &nbsp;\u00b7&nbsp; 수령인 연락처 &nbsp;\u00b7&nbsp; 개인통관고유부호
      </div>
      <div style="font-size:19px;color:#555;line-height:1.8">
        하나라도 다르면 통관이 되지 않습니다.<br>
        발급 당시와 정보가 달라졌다면 관세청에서 재발급 후 주문해 주세요.<br>
        <span style="font-size:17px;color:#999">개인통관고유부호가 없으시면 관세청 홈페이지에서 무료로 발급받으실 수 있습니다.</span>
      </div>
    </div>
  </div>

  <div style="background:#fff;border-radius:16px;margin-bottom:16px;overflow:hidden">
    <div style="background:#F7F5F0;padding:18px 28px;font-size:24px;font-weight:700;color:#1B3A5C;border-bottom:2px solid #E8845A">주문 취소</div>
    <div style="padding:24px 28px;font-size:19px;color:#555;line-height:1.8">
      \u00b7 주문 당일 <span style="color:#D94040;font-weight:600">오후 6시까지</span> 취소하실 수 있습니다. 오후 6시 이후 주문은 <span style="color:#D94040;font-weight:600">당일 자정 이전까지</span> 가능합니다.<br>
      \u00b7 해외 발주가 완료된 이후의 취소는 <b>반품으로 처리</b>됩니다.<br>
      \u00b7 <b>구매 확정 이후에는 취소가 불가</b>합니다.<br>
      \u00b7 배송 지연으로 인한 취소는 주문일로부터 <span style="color:#D94040;font-weight:600">30일 초과 시</span> 가능합니다.
    </div>
  </div>

  <div style="background:#fff;border-radius:16px;margin-bottom:16px;overflow:hidden">
    <div style="background:#F7F5F0;padding:18px 28px;font-size:24px;font-weight:700;color:#1B3A5C;border-bottom:2px solid #E8845A">반품 / 교환 기준</div>
    <div style="padding:24px 28px;font-size:19px;color:#555;line-height:1.8">
      상품 수령 후 <span style="color:#D94040;font-weight:600">7일 이내</span>에 신청하실 수 있습니다.
      표시\u00b7광고 내용과 다르거나 계약과 다르게 이행된 경우에는 그 사실을 안 날부터 <b>30일 이내</b>에 교환\u00b7반품이 가능합니다.<br>
      <span style="color:#1B3A5C;font-weight:600">해외 배송 상품 특성상 반품\u00b7교환 진행 시 추가 안내를 드릴 수 있습니다.</span><br><br>

      <span style="font-size:20px;font-weight:700;color:#1B3A5C">반품 시 비용 안내</span><br>
      \u00b7 출고 이후 취소하실 경우 <span style="color:#D94040;font-weight:600">배송비 차감 및 물류 수수료</span>가 발생할 수 있습니다.<br>
      \u00b7 해외 발주 이후 단순 변심으로 취소\u00b7반품하실 경우 반품 수수료가 발생합니다.<br>
      \u00b7 분리 배송된 상품을 고객님 사유로 반품하실 경우 박스별 반품 배송비가 부과됩니다.<br><br>

      <span style="font-size:20px;font-weight:700;color:#1B3A5C">반품 / 교환이 불가한 경우</span><br>
      \u00b7 고객님의 책임 있는 사유로 상품이 멸실\u00b7훼손된 경우 (상품 확인을 위한 포장 훼손은 제외)<br>
      \u00b7 사용 또는 소비로 상품의 가치가 현저히 감소한 경우<br>
      \u00b7 시간이 지나 재판매가 곤란할 정도로 가치가 감소한 경우<br>
      \u00b7 복제가 가능한 상품의 포장을 훼손한 경우<br>
      \u00b7 주문에 따라 개별 생산되는 상품의 제작이 시작된 경우<br>
      \u00b7 <b>재포장이 불가하거나 재포장되지 않은 경우</b><br>
      \u00b7 <b>제공된 구성품을 분실하거나 누락한 경우</b><br>
      \u00b7 <b>식품을 섭취한 흔적이 있는 경우</b>
    </div>
  </div>

  <div style="background:#fff;border-radius:16px;margin-bottom:16px;overflow:hidden">
    <div style="background:#F7F5F0;padding:18px 28px;font-size:24px;font-weight:700;color:#1B3A5C;border-bottom:2px solid #E8845A">A/S 안내</div>
    <div style="padding:24px 28px;font-size:19px;color:#555;line-height:1.8">
      해외 구매대행 상품 특성상 <span style="color:#D94040;font-weight:600">국내 A/S는 제공되지 않습니다.</span>
    </div>
  </div>

</div>
<div style="padding:26px 40px 44px;background:#15304D">
  <div style="background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.15);border-radius:12px;padding:22px 26px;font-size:15px;color:rgba(255,255,255,0.62);line-height:1.75">
    <span style="color:rgba(255,255,255,0.88);font-weight:700;font-size:16px">이미지 \u00b7 정보 저작권 안내</span><br>
    본 페이지의 <b style="color:rgba(255,255,255,0.8)">디자인\u00b7구성\u00b7문구는 당사가 자체 제작</b>하였으며, 제품 사진은 상품 안내를 위해 인용하였습니다. 권리 관련 문의는 <b style="color:rgba(255,255,255,0.8)">문의하기</b>로 연락 주시면 즉시 확인 후 조치하겠습니다.
  </div>
</div>"""

PA_SECTION_AMAZON_NOTICE = """<div style="background:linear-gradient(135deg,#1B3A5C 0%,#0F2640 100%);padding:48px 40px 42px;text-align:center">
  <div style="display:inline-block;background:rgba(232,132,90,0.15);border:2px solid rgba(232,132,90,0.45);border-radius:30px;padding:10px 28px;font-size:22px;font-weight:700;color:#fff;margin-bottom:18px">\U0001F30D 해외 구매대행 상품</div>
  <p style="font-size:24px;font-weight:600;color:#fff;line-height:1.7;margin:0">
    고객님을 대신해 <span style="color:#E8845A;font-weight:800">미국 아마존</span> 등 해외 정식 판매처에서 구매하여<br>
    안전하게 배송해 드립니다.
  </p>
</div>
<div style="background:#fff;padding:28px 40px;border-left:4px solid #E8845A;border-right:4px solid #E8845A">
  <div style="background:#FDF6F2;border-radius:14px;padding:24px 28px;margin-bottom:20px;border:1px solid #F5D9CB">
    <div style="font-size:20px;font-weight:700;color:#1B3A5C;margin-bottom:10px">\U0001F4E6 파손 / 오배송 안내</div>
    <p style="font-size:18px;color:#555;line-height:1.8;margin:0">
      받아보신 상품이 파손 또는 오배송된 경우 <span style="color:#E8845A;font-weight:600;text-decoration:underline;text-underline-offset:3px">문의하기</span>로 사진을 첨부해 주시면<br>
      불편함 없이 환불받으실 수 있도록 도와드리겠습니다.
    </p>
  </div>
  <div style="background:#F5F8FC;border-radius:14px;padding:24px 28px;border:1px solid #D6E4F0">
    <div style="font-size:20px;font-weight:700;color:#1B3A5C;margin-bottom:10px">\U0001F30D 해외 상품 특성</div>
    <p style="font-size:18px;color:#555;line-height:1.8;margin:0">
      미국 등 해외에서 판매되는 상품으로 <span style="color:#D94040;font-weight:600">사이즈, 사용 연령, 언어, A/S</span> 기준이 국내 제품과 다를 수 있습니다.
    </p>
  </div>
</div>
<div style="height:6px;background:linear-gradient(90deg,#E8845A,#1B3A5C)"></div>"""

PA_SECTIONS = ["auth", "shipping", "gallery", "amazon_notice", "notice"]

# 채널별 팔레트 — 조립된 HTML 의 브랜드색만 치환한다(의미색·회색은 보존).
from backend.purchase.services.detail_palette import recolor as _recolor  # noqa: E402


# ── SEO 제목 고유명사 게이트 (2026-08-08) ─────────────
# 차종/기기 제조사명은 원문에 있으면 SEO 제목에도 남아야 한다.
_MAKES = (
    "subaru", "toyota", "honda", "nissan", "mazda", "mitsubishi", "lexus", "infiniti", "acura",
    "hyundai", "kia", "genesis", "ford", "chevrolet", "chevy", "gmc", "cadillac", "buick",
    "jeep", "dodge", "chrysler", "tesla", "bmw", "mercedes", "benz", "audi",
    "volkswagen", "porsche", "volvo", "land rover", "jaguar",
    "iphone", "galaxy", "ipad", "macbook", "airpods", "pixel",
)
_MAKE_KO = {
    "subaru": ("스바루",), "toyota": ("도요타", "토요타"), "honda": ("혼다",),
    "nissan": ("닛산",), "mazda": ("마쓰다", "마즈다"), "lexus": ("렉서스",),
    "hyundai": ("현대",), "kia": ("기아",), "cadillac": ("캐딜락",),
    "volkswagen": ("폭스바겐",), "tesla": ("테슬라",), "audi": ("아우디",),
    "mercedes": ("벤츠",), "benz": ("벤츠",), "volvo": ("볼보",), "porsche": ("포르쉐",),
    "jeep": ("지프",), "ford": ("포드",), "chevrolet": ("쉐보레",),
    "iphone": ("아이폰",), "galaxy": ("갤럭시",), "ipad": ("아이패드",),
}


def _guard_proper_nouns(title_en: str, title_ko: str, seo_title: str, product_id) -> str:
    """원문의 차종/기기 제조사명이 SEO 제목에서 사라졌으면 SEO 제목을 폐기하고 번역본 반환."""
    if not title_en or not seo_title:
        return seo_title
    low_en, low_seo = title_en.lower(), seo_title.lower()
    for mk in _MAKES:
        if mk not in low_en or mk in low_seo:
            continue
        if any(ko in seo_title for ko in _MAKE_KO.get(mk, ())):
            continue
        logger.warning(
            "[seo-guard] product %s: 원문의 '%s' 가 SEO 제목에서 사라짐 — "
            "SEO 제목 폐기하고 번역본 사용. seo=%r", product_id, mk, seo_title)
        return title_ko
    return seo_title


# 상세 HTML 템플릿 버전. PA_SECTION_* 문구/구성을 고치면 이 값을 올린다 —
# detail_pages.template_version 과 비교해 구버전이면 다음 사용 시 자동 재생성된다.
# (2026-08-07: 아마존 제휴 오표기 제거 + 배너 정본 동기화)
PA_TEMPLATE_VERSION = "2026-08-08-p2"


def detail_is_current(product_id: int) -> bool:
    """현행 템플릿 버전의 상세 HTML 을 이미 갖고 있는가 — 재생성 트리거용.

    ★행 존재만 보던 종전 가드는 문구를 고쳐도 옛 HTML 을 그대로 재사용해서,
      구버전 문구가 신규 등록으로 계속 새어나갔다. 등록 가능 여부를 판단하는
      게이트에는 이 함수를 쓰지 말 것(존재 검사를 유지해야 한다).
    """
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM detail_pages WHERE product_id=? "
                "AND html_content IS NOT NULL AND html_content != '' "
                "AND template_version=? LIMIT 1",
                (product_id, PA_TEMPLATE_VERSION),
            ).fetchone()
        return bool(row)
    except Exception as e:
        # 컬럼 부재(마이그레이션 전) 등 → 종전 동작으로 폴백. 무한 재생성 방지.
        logger.warning(f"[detail-version] {product_id} 버전 검사 실패, 존재검사 폴백: {e}")
        with get_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM detail_pages WHERE product_id=? "
                "AND html_content IS NOT NULL AND html_content != '' LIMIT 1",
                (product_id,),
            ).fetchone()
        return bool(row)


def _agent_section_urls(product_id: int, platform: str) -> list[str]:
    """채널별 에이전트 섹션 이미지 URL. 없으면 빈 리스트(→ 원본 사진 폴백).

    쿠팡은 종전 파일명(seo_detail.json), 그 외 채널은 seo_detail_{channel}.json.
    """
    if not product_id:
        return []
    try:
        from pathlib import Path as _P
        import json as _j
        from backend_shared._config import PUBLIC_BASE_URL
        name = "seo_detail.json" if platform == "coupang" else f"seo_detail_{platform}.json"
        mf = (_P(__file__).resolve().parent.parent / "media" / "products"
              / str(product_id) / name)
        if not mf.is_file():
            return []
        urls = _j.loads(mf.read_text(encoding="utf-8")) or []
        base = (PUBLIC_BASE_URL or "").rstrip("/")
        out = []
        for u in urls:
            if not isinstance(u, str) or not u:
                continue
            out.append(u if u.startswith("http") else base + u)

        # ★쿠팡 contents 와 같은 순서로 꼬리 블록을 잇는다(2026-08-08).
        #   종전엔 에이전트 섹션에서 끝나 세부사항 스펙표가 네이버에만 빠져 있었다.
        media = mf.parent
        # 스펙표는 쿠팡 등록을 한 번도 안 탄 상품엔 파일이 없다 → 여기서 1회 생성.
        #   렌더 실패가 상세 생성을 막지 않도록 방어적으로 감싼다(AI 호출 없음, 0원).
        if not (media / "spec.jpg").is_file():
            try:
                from backend.purchase.services.spec_table import render_spec_table
                render_spec_table(product_id)
            except Exception as _e:
                logger.warning(f"[detail] {product_id} 스펙표 렌더 실패(계속): {_e}")
        for _n in ("components_cut.jpg", "components_block.jpg", "spec.jpg"):
            if (media / _n).is_file():
                out.append(f"{base}/api/pa/images/products/{product_id}/{_n}")
        return out
    except Exception as e:
        logger.warning(f"[detail] {product_id} {platform} 매니페스트 읽기 실패: {e}")
        return []


def _rich_alt(url: str, alt_base: str | None, idx: int) -> str:
    """리치 블록의 대체텍스트. 파일명으로 용도를 판별하고, 해당 없으면 순번."""
    s = str(url or "")
    nm = (alt_base or "").strip()
    if len(nm) > 50:
        nm = nm[:50].rstrip() + "…"
    pre = (nm + " ") if nm else ""
    if "/spec.jpg" in s:
        return (pre + "제품 사양표").strip()
    if "components_cut" in s:
        return (pre + "구성품 안내").strip()
    if "components_block" in s:
        return (pre + "구성품 상세").strip()
    return _alt_text(alt_base, idx)


def _alt_text(alt_base: str | None, idx: int) -> str:
    """이미지 대체텍스트. 장애인차별금지법·웹접근성지침 2.0 대응(2026-08-08).

    종전에는 모든 이미지에 "상품 이미지" 한 문구를 똑같이 박았다. 스크린리더로는
    "상품 이미지. 상품 이미지. 상품 이미지." 로 읽혀 정보량이 사실상 0이었다.
    상품명 + 순번으로 최소한의 식별이 되게 한다. AI 호출 없음 → 추가 비용 0원.
    """
    base = (alt_base or "").strip()
    if len(base) > 60:            # 스크린리더가 장문을 읽느라 막히지 않게 자른다
        base = base[:60].rstrip() + "…"
    if idx == 0:
        return f"{base} 대표 이미지".strip() if base else "대표 이미지"
    return f"{base} 상품 이미지 {idx + 1}".strip() if base else f"상품 이미지 {idx + 1}"


def _build_pa_html(image_urls: list[str], alt_base: str | None = None,
                   platform: str = "smartstore", product_id: int | None = None) -> str:
    """PA 전용 상세페이지 HTML 조립. 순서: 정품→배송→상품이미지→아마존안내→주의사항.

    alt_base : 대체텍스트에 쓸 상품명(없으면 순번만).
    platform : 채널별 팔레트 선택. 미지원 값이면 색을 바꾸지 않고 원본을 낸다.
    """
    # ★에이전트 리치 섹션 우선(2026-08-08). 없으면 종전대로 원본 사진 나열.
    _rich = _agent_section_urls(product_id, platform) if product_id else []
    if _rich:
        image_urls = _rich

    # 상품 이미지 태그 생성 — 2026-05-19: url escape 로 HTML inject 방어
    import html as _html
    if image_urls:
        img_tags = "\n".join(
            f'<img src="{_html.escape(url, quote=True)}" style="width:100%;display:block" '
            f'alt="{_html.escape(_rich_alt(url, alt_base, i), quote=True)}">'
            for i, url in enumerate(image_urls)
        )
    else:
        img_tags = '<div style="padding:40px;text-align:center;color:#999;font-size:18px">상품 이미지 없음</div>'

    gallery = PA_SECTION_GALLERY.replace("{{product_images}}", img_tags)

    parts = [
        '<div style="max-width:860px;margin:0 auto;font-family:\'Noto Sans KR\',sans-serif">',
        PA_SECTION_AUTH,
        PA_SECTION_SHIPPING,
        gallery,
        PA_SECTION_AMAZON_NOTICE,
        PA_SECTION_NOTICE,
        "</div>",
    ]
    return _recolor("\n".join(parts), platform)


def _save_detail_page_pa(product_id: int, html: str, sections_json: str,
                          market: str, platform: str) -> int:
    """PA database.get_db()로 detail_pages 저장. 동일 product+platform → UPDATE."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM detail_pages WHERE product_id=? AND platform=?",
            (product_id, platform),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE detail_pages
                   SET html_content=?, sections=?, market=?, status='draft',
                       template_version=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (html, sections_json, market, PA_TEMPLATE_VERSION, existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO detail_pages
               (product_id, sections, html_content, market, platform, status, template_version)
               VALUES (?,?,?,?,?,?,?)""",
            (product_id, sections_json, html, market, platform, "draft", PA_TEMPLATE_VERSION),
        )
        return cur.lastrowid



def ensure_detail_html(product_id: int, platform: str = "smartstore",
                       force: bool = False) -> int | None:
    """동기 상세 HTML 생성/저장 — 네이버 detailContent 용 단일 진입점(2026-08-07).

    Stage 1(process_product_html_only)의 async 이미지 다운로드 없이, 이미 받아둔
    image_cache 의 로컬 URL 만으로 조립한다. 동기 호출부(group_lister.ensure_promoted 등)
    에서 쓰라고 만든 폴백 경로 — 과거엔 여기서 backend_shared.detail_page_service 라는
    별개 엔진(12섹션 SECTION_HTML)을 불러 써서, 같은 상품이 경로에 따라 다른 상세를
    받는 문제가 있었다. 이제 전 경로가 _build_pa_html 한 벌로 통일된다.

    Returns: detail_pages.id (생성/갱신), 이미 있거나 이미지가 없으면 None.
    """
    if not force and detail_is_current(product_id):
        return None

    with get_db() as conn:
        rows = conn.execute(
            "SELECT public_url FROM image_cache WHERE product_id=? AND public_url IS NOT NULL "
            "ORDER BY image_idx",
            (product_id,),
        ).fetchall()
    image_urls = [r["public_url"] for r in rows if r["public_url"]]
    if not image_urls:
        logger.warning(f"[ensure-detail-html] {product_id} 로컬 이미지 없음 — 상세 생성 스킵")
        return None

    with get_db() as conn:      # alt 용 상품명 — 한글 우선, 없으면 영문
        _t = conn.execute(
            "SELECT COALESCE(NULLIF(title_ko,''), title_en) AS nm FROM products WHERE id=?",
            (product_id,),
        ).fetchone()
    html = _build_pa_html(image_urls, alt_base=(_t["nm"] if _t else None),
                          platform=platform, product_id=product_id)
    return _save_detail_page_pa(product_id, html, json.dumps(PA_SECTIONS), "KR", platform)

async def process_product(product_id: int, platform: str = "smartstore", force: bool = False) -> dict:
    """단일 상품 AI 처리 파이프라인. force=False이면 이미 처리된 상품은 스킵."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        raise ValueError(f"product {product_id} 없음")
    row = dict(row)

    if not force and row.get("ai_processed_at"):
        return {"product_id": product_id, "skipped": True, "reason": "이미 처리됨"}

    title_en = row.get("title_en") or ""
    if not title_en:
        raise ValueError(f"product {product_id}: title_en 없음 — 번역 불가")

    # 0. 이미지 수집 — images_json이 부족하면 Amazon에서 전체 이미지 크롤링
    images_json = row.get("images_json") or "[]"
    try:
        existing_urls = json.loads(images_json) if images_json else []
    except (json.JSONDecodeError, TypeError):
        existing_urls = []

    asin = row.get("asin") or ""
    if len(existing_urls) < 3 and asin:
        logger.info(f"[product {product_id}] 이미지 {len(existing_urls)}장 → Amazon에서 전체 수집 시도")
        amazon_urls = fetch_amazon_images(asin)
        if amazon_urls:
            # 기존 URL + 신규 URL 합치기 (중복 제거)
            merged = list(dict.fromkeys(existing_urls + amazon_urls))
            images_json = json.dumps(merged, ensure_ascii=False)
            # products 테이블에도 업데이트
            with get_db() as conn:
                conn.execute(
                    "UPDATE products SET images_json=? WHERE id=?",
                    (images_json, product_id),
                )
            logger.info(f"[product {product_id}] 이미지 보충 완료: {len(existing_urls)} → {len(merged)}장")

    description_en = row.get("description_en") or ""
    existing_cat = row.get("category_path") or ""

    # 1단계 병렬: 이미지 다운로드 + 제목/설명 번역 동시 (서로 독립)
    title_task = translate_text(title_en, "en", "ko")
    desc_task = translate_text(description_en, "en", "ko") if description_en else None
    img_task = download_product_images(product_id, images_json)

    tasks = [img_task, title_task] + ([desc_task] if desc_task else [])
    results = await asyncio.gather(*tasks)
    img_result = results[0]
    tr_title = results[1]
    tr_desc = results[2] if desc_task else None

    # 이미지 0장이면 ai_processed_at 커밋 없이 조기 실패 → 재시도 대상으로 유지.
    # 커밋되면 products.status가 pending으로 넘어가 쿠팡 업로드 단계에서 '이미지 없음'으로 실패한다.
    downloaded = img_result.get("downloaded", 0) if isinstance(img_result, dict) else 0
    if downloaded == 0:
        raise RuntimeError(
            f"이미지 다운로드 0장 — ai_processed_at 커밋 스킵 (재시도 필요). "
            f"images_json={len(json.loads(images_json) if images_json else [])}장"
        )

    title_ko = tr_title["translated"]
    description_ko = tr_desc["translated"] if tr_desc else None

    # 2단계 병렬: SEO + 카테고리 매핑 (둘 다 title_ko 에 의존)
    seo_task = generate_seo(
        product_name=title_ko,
        category=existing_cat,
        market="KR",
        platform=platform,
        description=description_ko or "",
    )
    cat_task = None if existing_cat.isdigit() else resolve_category(
        product_name=title_ko,
        source_hint=existing_cat,
    )
    stage2 = [seo_task] + ([cat_task] if cat_task else [])
    stage2_results = await asyncio.gather(*stage2)
    seo_result = stage2_results[0]
    cat_result = stage2_results[1] if cat_task else None

    seo_title = seo_result.get("optimized_title") or title_ko
    # ★고유명사 검증(2026-08-08) — SEO 최적화가 차종/기기명을 바꾸는 사고 방지
    seo_title = _guard_proper_nouns(title_en or "", title_ko, seo_title, product_id)
    if len(title_ko) > 50:
        title_ko = seo_title[:50] if len(seo_title) <= 50 else seo_title[:47] + "..."
    seo_tags_list = seo_result.get("tags") or seo_result.get("keywords") or []
    seo_tags = json.dumps(seo_tags_list, ensure_ascii=False) if seo_tags_list else "[]"

    mapped_category = existing_cat if cat_result is None else (cat_result.get("mapped_category") or existing_cat)

    # ── 효능 표현 정제 (건강식품 카테고리만) ──
    if clean_policy.is_health_food_category(mapped_category):
        before_title = title_ko
        before_seo = seo_title
        before_desc = description_ko
        title_ko = clean_policy.sanitize_efficacy_claims(title_ko)
        seo_title = clean_policy.sanitize_efficacy_claims(seo_title)
        if description_ko:
            description_ko = clean_policy.sanitize_efficacy_claims(description_ko)
        if title_ko != before_title:
            clean_policy.log_violation(
                stage='ai', violation_type='efficacy_claim', action_taken='sanitized',
                product_id=product_id, channel=None,
                original_text=before_title, notes=f'title_ko 정제: "{before_title}" → "{title_ko}"',
            )
        if seo_title != before_seo:
            clean_policy.log_violation(
                stage='ai', violation_type='efficacy_claim', action_taken='sanitized',
                product_id=product_id, channel=None,
                original_text=before_seo, notes=f'seo_title 정제',
            )

    # 4. HTML 생성 (PA 전용 템플릿)
    image_urls = img_result.get("local_urls") or []
    html = _build_pa_html(image_urls, alt_base=title_ko, platform=platform,
                          product_id=product_id)

    # 5. products 테이블 업데이트
    with get_db() as conn:
        conn.execute(
            """UPDATE products SET
                   title_ko=?, description_ko=?, seo_title=?, seo_tags=?,
                   category_path=COALESCE(?, category_path),
                   ai_processed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (title_ko, description_ko, seo_title, seo_tags, mapped_category, product_id),
        )

    # 6. detail_pages 저장
    sections_json = json.dumps(PA_SECTIONS)
    detail_page_id = _save_detail_page_pa(product_id, html, sections_json, "KR", platform)

    return {
        "product_id": product_id,
        "title_ko": title_ko,
        "seo_title": seo_title,
        "seo_tags": seo_tags_list,
        "category": mapped_category,
        "html_length": len(html),
        "detail_page_id": detail_page_id,
    }


async def process_batch(product_ids: list[int], platform: str = "smartstore", concurrency: int | None = None):
    """여러 상품을 Semaphore 병렬로 AI 처리. 완료 순서대로 yield (SSE용).

    concurrency: 동시 처리 상품 수. 환경변수 AI_BATCH_CONCURRENCY 기본 8."""
    total = len(product_ids)
    if concurrency is None:
        import os
        concurrency = int(os.environ.get("AI_BATCH_CONCURRENCY", "8"))
    sem = asyncio.Semaphore(max(1, concurrency))

    async def run_one(pid: int):
        async with sem:
            try:
                result = await process_product(pid, platform)
                return {"pid": pid, "ok": True, "title_ko": result.get("title_ko", "")}
            except Exception as e:
                logger.warning(f"[ai-processor] product {pid} 실패: {e}")
                return {"pid": pid, "ok": False, "error": str(e)}

    tasks = [asyncio.create_task(run_one(p)) for p in product_ids]
    processed = 0
    errors = 0
    done = 0

    for fut in asyncio.as_completed(tasks):
        res = await fut
        done += 1
        if res["ok"]:
            processed += 1
            yield {
                "current": done,
                "total": total,
                "pct": round(done / total * 100, 1),
                "product_id": res["pid"],
                "title_ko": res["title_ko"],
                "status": "ok",
            }
        else:
            errors += 1
            yield {
                "current": done,
                "total": total,
                "pct": round(done / total * 100, 1),
                "product_id": res["pid"],
                "status": "error",
                "message": res["error"],
            }

    yield {
        "event": "done",
        "processed": processed,
        "errors": errors,
        "total": total,
    }


# ── 백그라운드 큐 방식 ──────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_batch_job(product_ids: list[int], platform: str = "smartstore") -> str:
    """batch_jobs 레코드 생성, job_id 반환."""
    job_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, created_at)
               VALUES (?, 'ai_detail', 'pending', ?, ?)""",
            (job_id, len(product_ids), _now_iso()),
        )
    return job_id


def get_batch_job(job_id: str) -> dict | None:
    """batch_jobs 상태 조회."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_running_job() -> dict | None:
    """현재 실행 중인 ai_detail job 조회 (running 또는 pending).

    job_type 필터 필수 — batch_jobs 에는 coupang_order_sync/smartstore_order_sync 등
    폴러 row 도 들어가서 ai_detail 이 가려질 수 있다.
    """
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM batch_jobs
               WHERE status IN ('pending','running') AND job_type='ai_detail'
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
    return dict(row) if row else None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2-stage 분리 처리 (Option B)
# Stage 1: 이미지 다운로드 + HTML 생성 + detail_pages INSERT (Gemini 호출 0)
# Stage 2: 번역 + SEO + 카테고리 매핑 + products UPDATE (Gemini 호출 3~4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def process_product_html_only(product_id: int, platform: str = "smartstore",
                                     force: bool = False) -> dict:
    """Stage 1 — 이미지 다운로드 + HTML 생성 + detail_pages INSERT.
    Gemini 호출 X. 빠른 처리.
    """
    with get_db() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        raise ValueError(f"product {product_id} 없음")
    row = dict(row)

    # 이미 detail_pages 가 있고 force=False 면 skip
    if not force and detail_is_current(product_id):
        return {"product_id": product_id, "skipped": True, "reason": "detail_pages 최신 버전 보유"}

    # 이미지 보충 (부족하면 Amazon 크롤링)
    images_json = row.get("images_json") or "[]"
    try:
        existing_urls = json.loads(images_json) if images_json else []
    except (json.JSONDecodeError, TypeError):
        existing_urls = []
    asin = row.get("asin") or ""
    if len(existing_urls) < 3 and asin:
        # 2026-05-19: sync requests.get 을 to_thread 로 감싸 event loop blocking 회피
        amazon_urls = await asyncio.to_thread(fetch_amazon_images, asin)
        if amazon_urls:
            merged = list(dict.fromkeys(existing_urls + amazon_urls))
            images_json = json.dumps(merged, ensure_ascii=False)
            with get_db() as conn:
                conn.execute(
                    "UPDATE products SET images_json=? WHERE id=?",
                    (images_json, product_id),
                )

    # 이미지 다운로드
    img_result = await download_product_images(product_id, images_json)
    downloaded = img_result.get("downloaded", 0) if isinstance(img_result, dict) else 0
    if downloaded == 0:
        raise RuntimeError(
            f"이미지 다운로드 0장 — Stage 1 실패 (재시도 필요). "
            f"images_json={len(json.loads(images_json) if images_json else [])}장"
        )

    # HTML 생성 + detail_pages INSERT
    image_urls = img_result.get("local_urls") or []
    with get_db() as conn:      # Stage 1 은 번역 전 → 한글 없으면 영문으로 대체
        _t = conn.execute(
            "SELECT COALESCE(NULLIF(title_ko,''), title_en) AS nm FROM products WHERE id=?",
            (product_id,),
        ).fetchone()
    html = _build_pa_html(image_urls, alt_base=(_t["nm"] if _t else None),
                          platform=platform, product_id=product_id)
    sections_json = json.dumps(PA_SECTIONS)
    detail_page_id = _save_detail_page_pa(product_id, html, sections_json, "KR", platform)

    return {
        "product_id": product_id,
        "detail_page_id": detail_page_id,
        "image_count": downloaded,
        "html_length": len(html),
    }


async def process_product_ai_only(product_id: int, platform: str = "smartstore",
                                   force: bool = False) -> dict:
    """Stage 2 — 번역 + SEO + 카테고리 매핑 + products UPDATE.
    Gemini 호출 3~4 (title 번역, desc 번역, SEO, 카테고리 매핑).
    """
    with get_db() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        raise ValueError(f"product {product_id} 없음")
    row = dict(row)

    if not force and row.get("ai_processed_at"):
        return {"product_id": product_id, "skipped": True, "reason": "이미 AI 처리됨"}

    title_en = row.get("title_en") or ""
    if not title_en:
        raise ValueError(f"product {product_id}: title_en 없음")

    description_en = row.get("description_en") or ""
    existing_cat = row.get("category_path") or ""

    # 1단계 병렬: title + description 번역
    title_task = translate_text(title_en, "en", "ko")
    desc_task = translate_text(description_en, "en", "ko") if description_en else None
    tasks = [title_task] + ([desc_task] if desc_task else [])
    results = await asyncio.gather(*tasks)
    tr_title = results[0]
    tr_desc = results[1] if desc_task else None
    title_ko = tr_title["translated"]
    description_ko = tr_desc["translated"] if tr_desc else None

    # 2단계 병렬: SEO + 카테고리 매핑 (existing_cat 이 숫자면 카테고리 skip)
    seo_task = generate_seo(
        product_name=title_ko,
        category=existing_cat,
        market="KR",
        platform=platform,
        description=description_ko or "",
    )
    cat_task = None if existing_cat.isdigit() else resolve_category(
        product_name=title_ko, source_hint=existing_cat,
    )
    stage2 = [seo_task] + ([cat_task] if cat_task else [])
    stage2_results = await asyncio.gather(*stage2)
    seo_result = stage2_results[0]
    cat_result = stage2_results[1] if cat_task else None

    seo_title = seo_result.get("optimized_title") or title_ko
    # ★고유명사 검증(2026-08-08) — SEO 최적화가 차종/기기명을 바꾸는 사고 방지
    seo_title = _guard_proper_nouns(title_en or "", title_ko, seo_title, product_id)
    if len(title_ko) > 50:
        title_ko = seo_title[:50] if len(seo_title) <= 50 else seo_title[:47] + "..."
    seo_tags_list = seo_result.get("tags") or seo_result.get("keywords") or []
    seo_tags = json.dumps(seo_tags_list, ensure_ascii=False) if seo_tags_list else "[]"
    mapped_category = existing_cat if cat_result is None else (
        cat_result.get("mapped_category") or existing_cat
    )

    # ── 효능 표현 정제 (건강식품 카테고리만) ──
    if clean_policy.is_health_food_category(mapped_category):
        before_t = title_ko
        title_ko = clean_policy.sanitize_efficacy_claims(title_ko)
        seo_title = clean_policy.sanitize_efficacy_claims(seo_title)
        if description_ko:
            description_ko = clean_policy.sanitize_efficacy_claims(description_ko)
        if title_ko != before_t:
            clean_policy.log_violation(
                stage='ai', violation_type='efficacy_claim', action_taken='sanitized',
                product_id=product_id, original_text=before_t,
                notes='ai_only title_ko 정제',
            )

    # products UPDATE — ai_processed_at 도 설정
    with get_db() as conn:
        conn.execute(
            """UPDATE products SET
                  title_ko=?, description_ko=?, seo_title=?, seo_tags=?,
                  category_path=COALESCE(?, category_path),
                  ai_processed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (title_ko, description_ko, seo_title, seo_tags, mapped_category, product_id),
        )

    return {
        "product_id": product_id,
        "title_ko": title_ko,
        "seo_title": seo_title,
        "category_path": mapped_category,
    }


async def run_two_stage_batch(job_id: str, product_ids: list[int], platform: str = "smartstore", yield_unit=None):
    """2-stage batch — Stage 1 (HTML) 전체 → Stage 2 (AI) 전체 순차 실행.
    phase_message 에 JSON 으로 두 stage 진행도 저장.

    yield_unit: turnstile SHARED 락 컨텍스트(async_shared_unit). 주어지면 청크 경계마다
    배치잡(writer) 대기 시 락을 양보 → 긴 detailing 단계가 타임잡을 굶기지 않음.
    """
    import os

    s1_concurrency = int(os.environ.get("AI_BATCH_HTML_CONCURRENCY", "8"))
    s2_concurrency = int(os.environ.get("AI_BATCH_CONCURRENCY", "4"))
    lock_chunk = int(os.environ.get("AI_BATCH_LOCK_CHUNK", "40"))

    total = len(product_ids)
    state = {"stage": 1,
             "s1": {"processed": 0, "total": total, "errors": 0, "skipped": 0},
             "s2": {"processed": 0, "total": total, "errors": 0, "skipped": 0}}
    state_lock = asyncio.Lock()

    def _save_state():
        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET phase_message=? WHERE id=?",
                (json.dumps(state, ensure_ascii=False), job_id),
            )

    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET status='running', started_at=?, phase_message=? WHERE id=?",
            (_now_iso(), json.dumps(state, ensure_ascii=False), job_id),
        )

    # ── Stage 1: HTML ──
    sem1 = asyncio.Semaphore(max(1, s1_concurrency))

    async def run_s1(pid: int):
        async with sem1:
            try:
                r = await process_product_html_only(pid, platform)
                async with state_lock:
                    if r.get("skipped"):
                        state["s1"]["skipped"] += 1
                    else:
                        state["s1"]["processed"] += 1
            except Exception as e:
                async with state_lock:
                    state["s1"]["errors"] += 1
                logger.warning(f"[two-stage {job_id}] S1 product {pid}: {e}")
            # DB UPDATE — 매 5건마다 또는 마지막 (sqlite 충돌 방지)
            async with state_lock:
                done = state["s1"]["processed"] + state["s1"]["skipped"] + state["s1"]["errors"]
                if done % 5 == 0 or done == total:
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE batch_jobs SET processed=?, errors=?, current_product_id=?, phase_message=? WHERE id=?",
                            (state["s1"]["processed"] + state["s1"]["skipped"],
                             state["s1"]["errors"] + state["s2"]["errors"],
                             pid, json.dumps(state, ensure_ascii=False), job_id),
                        )

    for _i in range(0, len(product_ids), lock_chunk):
        await asyncio.gather(*[run_s1(p) for p in product_ids[_i:_i + lock_chunk]], return_exceptions=False)
        if yield_unit is not None:
            await yield_unit.checkpoint()
    logger.info(f"[two-stage {job_id}] Stage 1 완료 — {state['s1']}")

    # ── Stage 2: AI ──
    state["stage"] = 2
    _save_state()
    sem2 = asyncio.Semaphore(max(1, s2_concurrency))

    async def run_s2(pid: int):
        async with sem2:
            try:
                r = await process_product_ai_only(pid, platform)
                async with state_lock:
                    if r.get("skipped"):
                        state["s2"]["skipped"] += 1
                    else:
                        state["s2"]["processed"] += 1
            except Exception as e:
                async with state_lock:
                    state["s2"]["errors"] += 1
                logger.warning(f"[two-stage {job_id}] S2 product {pid}: {e}")
            # DB UPDATE — 매 5건마다 또는 마지막
            async with state_lock:
                done = state["s2"]["processed"] + state["s2"]["skipped"] + state["s2"]["errors"]
                if done % 5 == 0 or done == total:
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE batch_jobs SET processed=?, errors=?, current_product_id=?, phase_message=? WHERE id=?",
                            (total + state["s2"]["processed"] + state["s2"]["skipped"],
                             state["s1"]["errors"] + state["s2"]["errors"],
                             pid, json.dumps(state, ensure_ascii=False), job_id),
                        )

    for _i in range(0, len(product_ids), lock_chunk):
        await asyncio.gather(*[run_s2(p) for p in product_ids[_i:_i + lock_chunk]], return_exceptions=False)
        if yield_unit is not None:
            await yield_unit.checkpoint()

    state["stage"] = 3  # 완료
    with get_db() as conn:
        conn.execute(
            """UPDATE batch_jobs SET status='done', processed=?, errors=?,
                   finished_at=?, current_product_id=NULL, phase_message=?
               WHERE id=?""",
            (
                total * 2,
                state["s1"]["errors"] + state["s2"]["errors"],
                _now_iso(),
                json.dumps(state, ensure_ascii=False),
                job_id,
            ),
        )
    logger.info(f"[two-stage {job_id}] 완료 — S1 {state['s1']}, S2 {state['s2']}")


async def run_batch_background(job_id: str, product_ids: list[int], platform: str = "smartstore"):
    """백그라운드 asyncio task로 Semaphore 병렬 실행. 진행률을 batch_jobs 에 기록.

    AI_BATCH_CONCURRENCY 환경변수로 동시 처리 상품 수 조절 (기본 8)."""
    import os
    concurrency = int(os.environ.get("AI_BATCH_CONCURRENCY", "8"))
    sem = asyncio.Semaphore(max(1, concurrency))
    counter_lock = asyncio.Lock()

    total = len(product_ids)
    processed = 0
    errors = 0

    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET status='running', started_at=? WHERE id=?",
            (_now_iso(), job_id),
        )

    skipped = 0

    async def run_one(pid: int):
        nonlocal processed, errors, skipped
        async with sem:
            try:
                result = await process_product(pid, platform)
                async with counter_lock:
                    if result.get("skipped"):
                        skipped += 1
                    else:
                        processed += 1
            except Exception as e:
                async with counter_lock:
                    errors += 1
                logger.warning(f"[batch-job {job_id}] product {pid} 실패: {e}")

            async with counter_lock:
                with get_db() as conn:
                    conn.execute(
                        """UPDATE batch_jobs
                           SET processed=?, errors=?, current_product_id=?
                           WHERE id=?""",
                        (processed + skipped, errors, pid, job_id),
                    )

    await asyncio.gather(*[run_one(p) for p in product_ids], return_exceptions=False)

    with get_db() as conn:
        conn.execute(
            """UPDATE batch_jobs
               SET status='done', processed=?, errors=?, finished_at=?,
                   current_product_id=NULL
               WHERE id=?""",
            (processed + skipped, errors, _now_iso(), job_id),
        )
    logger.info(f"[batch-job {job_id}] 완료 — 신규 {processed}, 스킵 {skipped}, 실패 {errors}/{total}")
