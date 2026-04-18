"""PA 전용 AI 처리 파이프라인 — 번역 + SEO + 카테고리 + 상세페이지 HTML 생성."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from backend.purchase.database import get_db
from backend.purchase.services.image_downloader import download_product_images, fetch_amazon_images
from backend.purchase.services.category_rag import resolve_category
from backend_shared.ai import translate_text, generate_seo

logger = logging.getLogger(__name__)

# ── PA 전용 상세페이지 섹션 (인라인 스타일) ─────────────

PA_SECTION_AUTH = """<div style="background:linear-gradient(135deg,#1B3A5C 0%,#0F2640 100%);padding:60px 40px 50px;text-align:center">
  <div style="margin-bottom:40px">
    <div style="font-size:41px;font-weight:800;color:#fff;letter-spacing:2px">Charis G</div>
    <div style="font-size:16px;color:rgba(255,255,255,0.5);letter-spacing:4px;margin-top:-2px">GLOBAL SOURCING</div>
  </div>
  <div style="width:100px;height:100px;margin:0 auto 30px;background:linear-gradient(145deg,#F5D77A,#D4A843,#F5D77A);border-radius:50%;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 20px rgba(212,168,67,0.4)">
    <span style="font-size:20px;font-weight:800;color:#5C3D0E;text-align:center;line-height:1.3">정품<br>인증</span>
  </div>
  <div style="font-size:46px;font-weight:800;color:#fff;margin-bottom:10px">본 제품은 <span style="color:#E8845A">100% 정품</span>입니다.</div>
  <p style="font-size:22px;color:rgba(255,255,255,0.6);line-height:1.8;margin-bottom:10px">공식 판매처의 정식 유통 제품만 취급합니다.<br>OEM 제품 / 가짜 상품을 절대 판매하지 않습니다.</p>
  <span style="display:inline-block;font-size:20px;font-weight:600;color:#E8845A;border-bottom:1px solid rgba(232,132,90,0.4);padding-bottom:2px">해외에서 A/S 가능한 정품만을 판매합니다.</span>
</div>
<div style="background:#F7F5F0;padding:50px 40px;text-align:center">
  <div style="margin-bottom:36px">
    <div style="font-size:18px;color:#E8845A;font-weight:600;letter-spacing:2px;margin-bottom:8px">WHY CHARIS G</div>
    <div style="font-size:36px;font-weight:800;color:#1B3A5C">Charis G를 선택해야 하는 <span style="color:#E8845A">4가지 이유</span></div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;max-width:680px;margin:0 auto">
    <div style="background:#fff;border-radius:16px;padding:32px 24px 28px;box-shadow:0 2px 12px rgba(0,0,0,0.06)">
      <span style="display:inline-block;width:28px;height:28px;background:#E8845A;border-radius:50%;color:#fff;font-size:16px;font-weight:700;line-height:28px;margin-bottom:16px">1</span>
      <span style="font-size:58px;margin-bottom:14px;display:block">🏢</span>
      <div style="font-size:23px;font-weight:700;color:#1B3A5C;margin-bottom:6px">검증된 글로벌 소싱</div>
      <div style="font-size:18px;color:#888;line-height:1.5">해외 공식 유통망을 통한<br>신뢰할 수 있는 제품 확보</div>
    </div>
    <div style="background:#fff;border-radius:16px;padding:32px 24px 28px;box-shadow:0 2px 12px rgba(0,0,0,0.06)">
      <span style="display:inline-block;width:28px;height:28px;background:#E8845A;border-radius:50%;color:#fff;font-size:16px;font-weight:700;line-height:28px;margin-bottom:16px">2</span>
      <span style="font-size:58px;margin-bottom:14px;display:block">💬</span>
      <div style="font-size:23px;font-weight:700;color:#1B3A5C;margin-bottom:6px">빠른 고객 응대</div>
      <div style="font-size:18px;color:#888;line-height:1.5">늦은 시간에도<br>신속한 1:1 상담 지원</div>
    </div>
    <div style="background:#fff;border-radius:16px;padding:32px 24px 28px;box-shadow:0 2px 12px rgba(0,0,0,0.06)">
      <span style="display:inline-block;width:28px;height:28px;background:#E8845A;border-radius:50%;color:#fff;font-size:16px;font-weight:700;line-height:28px;margin-bottom:16px">3</span>
      <span style="font-size:58px;margin-bottom:14px;display:block">📦</span>
      <div style="font-size:23px;font-weight:700;color:#1B3A5C;margin-bottom:6px">꼼꼼한 검수 포장</div>
      <div style="font-size:18px;color:#888;line-height:1.5">파손 걱정 NO<br>모든 제품 박스 포장 출고</div>
    </div>
    <div style="background:#fff;border-radius:16px;padding:32px 24px 28px;box-shadow:0 2px 12px rgba(0,0,0,0.06)">
      <span style="display:inline-block;width:28px;height:28px;background:#E8845A;border-radius:50%;color:#fff;font-size:16px;font-weight:700;line-height:28px;margin-bottom:16px">4</span>
      <span style="font-size:58px;margin-bottom:14px;display:block">🚚</span>
      <div style="font-size:23px;font-weight:700;color:#1B3A5C;margin-bottom:6px">빠르고 안전한 배송</div>
      <div style="font-size:18px;color:#888;line-height:1.5">검증된 국내 택배사를 통한<br>안전하고 빠른 배송</div>
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

PA_SECTION_NOTICE = """<div style="background:#1B3A5C;padding:50px 40px 30px;text-align:center">
  <div style="font-size:31px;font-weight:800;color:#fff;letter-spacing:1px;margin-bottom:20px">Charis G</div>
  <div style="display:inline-block;background:#E8845A;border-radius:30px;padding:10px 28px;font-size:20px;font-weight:700;color:#fff;margin-bottom:8px">해외 구매대행 상품</div>
  <div style="font-size:41px;font-weight:800;color:#fff;margin-bottom:6px">구매 전 필독 사항</div>
  <div style="font-size:19px;color:rgba(255,255,255,0.5);margin-bottom:30px">주문 하시기 전에 꼭 필독해주세요!</div>
</div>
<div style="background:#15304D;padding:0 40px 16px">
  <div style="background:#fff;border-radius:16px;margin-bottom:16px;overflow:hidden">
    <div style="background:#F7F5F0;padding:18px 28px;font-size:24px;font-weight:700;color:#1B3A5C;border-bottom:2px solid #E8845A">관부가세 안내</div>
    <div style="padding:24px 28px;font-size:19px;color:#555;line-height:1.8">
      · 관부가세 포함으로 표기된 상품 이외에는 <span style="color:#E8845A;font-weight:600;text-decoration:underline;text-underline-offset:3px">관부가세 미포함</span> 상품입니다.<br>
      · 구매금액이 미화 <span style="color:#D94040;font-weight:600">150달러를 초과</span>할 경우 별도로 관부가세가 발생합니다.<br>
      · 관부가세 미납으로 인한 통관지연 / 폐기처분은 당사에서 책임지지 않습니다.
    </div>
  </div>
  <div style="background:#fff;border-radius:16px;margin-bottom:16px;overflow:hidden">
    <div style="background:#F7F5F0;padding:18px 28px;font-size:24px;font-weight:700;color:#1B3A5C;border-bottom:2px solid #E8845A">주문 관련 안내</div>
    <div style="padding:24px 28px;font-size:19px;color:#555;line-height:1.8">
      · 해외 현지 발주 후 단순 변심으로 인한 주문 변경 / 취소, 반품 요청 시 <span style="color:#D94040;font-weight:600">별도 반품 수수료가 발생</span>하게 되므로, 신중히 결정 후 구매부탁드리겠습니다.<br>
      · 수령인의 주문 정보를 입력하실 때 개인통관고유부호 발급 시 기재했던 정보<br>
      <span style="color:#D94040;font-weight:600">수령인 성함 + 수령인 연락처 + 수령인 개인통관고유부호</span><br>
      위에 말씀드린 3개의 정보를 반드시 일치시켜 주세요.<br>
      <span style="font-size:16px;color:#999">*3가지 중 하나라도 맞지 않는다면 통관이 되지 않습니다.</span><br>
      <span style="font-size:16px;color:#999">*개인통관고유부호 발급 당시의 성함과 연락처 등의 정보가 바뀐 경우는 변경 &amp; 재발급을 부탁드립니다.</span>
    </div>
    <div style="background:#F0F7FF;border-radius:10px;padding:16px 24px;margin:0 28px 24px;border:1px solid #D0E3F7;text-align:center">
      <p style="font-size:16px;color:#555;margin:0 0 8px;font-weight:600">📋 개인통관고유부호가 없으신가요?</p>
      <p style="font-size:14px;color:#1B3A5C;font-weight:600;margin:0">관세청 개인통관고유부호 발급 바로가기 →</p>
      <p style="font-size:15px;color:#2B7CE9;font-weight:600;margin:8px 0 0;text-decoration:underline">unipass.customs.go.kr</p>
    </div>
  </div>
  <div style="background:#fff;border-radius:16px;margin-bottom:16px;overflow:hidden">
    <div style="background:#F7F5F0;padding:18px 28px;font-size:24px;font-weight:700;color:#1B3A5C;border-bottom:2px solid #E8845A">반품 / 교환 기준</div>
    <div style="padding:24px 28px;font-size:19px;color:#555;line-height:1.8">
      상품 수령 후 <span style="color:#D94040;font-weight:600">7일 이내</span>에 신청하실 수 있습니다.
      단, 제품이 표시·광고 내용과 다르거나 계약과 다르게 이행된 날부터 <b>30일 이내</b>에 교환/반품이 가능합니다.<br><br>
      <span style="font-size:20px;font-weight:700;color:#1B3A5C">반품 / 교환 불가 사유 (공통)</span><br>
      · 소비자의 책임 있는 사유로 상품 등이 멸실 또는 훼손된 경우 (단 상품 확인을 위한 포장 훼손 제외)<br>
      · 소비자의 사용 또는 소비에 의해 상품 등의 가치가 현저히 감소한 경우<br>
      · 시간 경과에 의해 재판매가 곤란할 정도로 상품 등의 가치가 현저히 감소한 경우<br>
      · 복제가 가능한 상품 등의 포장을 훼손한 경우<br>
      · 소비자의 주문에 따라 개별적으로 생산되는 상품이 제작에 들어간 경우<br><br>
      <span style="font-size:20px;font-weight:700;color:#1B3A5C">저온/신선 상품</span><br>
      · 고객 귀책 사유 (단순 변심, 주소 오기재, 주문 착오, 보관 부주의 및 상품 사용으로 가치가 하락한 경우 등)<br>
      · 다른 옵션 상품으로 교환을 요청하는 경우
    </div>
  </div>
  <div style="background:#fff;border-radius:16px;margin-bottom:16px;overflow:hidden">
    <div style="background:#F7F5F0;padding:18px 28px;font-size:24px;font-weight:700;color:#1B3A5C;border-bottom:2px solid #E8845A">A/S 안내 및 기타사항</div>
    <div style="padding:24px 28px;font-size:19px;color:#555;line-height:1.8">
      · 해외 구매대행의 상품 특성상 <span style="color:#D94040;font-weight:600">한국 내 A/S는 불가</span>합니다.<br>
      · 무게 및 부피가 큰 제품은 임의로 화물 택배사로 인계되어 배송될 수 있으며, 구매 시 결제하신 금액과는 <span style="color:#E8845A;font-weight:600;text-decoration:underline;text-underline-offset:3px">별도의 착불 운임</span>이 발생할 수 있습니다.
    </div>
  </div>
</div>
<div style="text-align:center;padding:30px 40px 40px;background:#1B3A5C">
  <div style="font-size:26px;font-weight:700;color:rgba(255,255,255,0.3);letter-spacing:4px">Charis G</div>
</div>"""

PA_SECTION_AMAZON_NOTICE = """<div style="background:linear-gradient(135deg,#232F3E 0%,#37475A 100%);padding:48px 40px 36px;text-align:center">
  <div style="margin-bottom:20px">
    <span style="font-size:42px;font-weight:800;color:#FF9900;letter-spacing:-1px">amazon</span>
    <span style="display:block;width:80px;height:5px;background:#FF9900;border-radius:3px;margin:6px auto 0"></span>
  </div>
  <div style="display:inline-block;background:rgba(255,153,0,0.15);border:2px solid rgba(255,153,0,0.4);border-radius:30px;padding:10px 28px;font-size:22px;font-weight:700;color:#fff;margin-bottom:16px">ℹ️ 아마존 상품 안내</div>
  <p style="font-size:24px;font-weight:600;color:#fff;line-height:1.7;margin:0">
    아마존 글로벌 스토어에서 판매 중인 상품으로<br>
    공식 판매자인 <span style="color:#FF9900;font-weight:800">아마존 미국</span>에서 판매/배송을 책임집니다.
  </p>
</div>
<div style="background:#fff;padding:28px 40px;border-left:4px solid #FF9900;border-right:4px solid #FF9900">
  <div style="background:#FFF8F0;border-radius:14px;padding:24px 28px;margin-bottom:20px;border:1px solid #FFE0B2">
    <div style="font-size:20px;font-weight:700;color:#232F3E;margin-bottom:10px">
      <span style="color:#FF9900;font-weight:800;margin-right:8px">amazon</span> 📦 파손 / 오배송 안내
    </div>
    <p style="font-size:18px;color:#555;line-height:1.8;margin:0 0 14px">
      받아보신 상품이 파손 또는 오배송 된 경우 <span style="color:#FF9900;font-weight:600;text-decoration:underline;text-underline-offset:3px">1:1 문의</span>로 사진을 첨부해 주시면<br>
      불편함 없이 환불받으실 수 있도록 도움드리겠습니다.
    </p>
  </div>
  <!-- 네이버 톡톡 배너 -->
  <div style="background:linear-gradient(135deg,#03C75A 0%,#02B550 100%);border-radius:14px;padding:24px 28px;margin-bottom:20px;text-align:center;border:1px solid #02A348">
    <div style="font-size:22px;font-weight:800;color:#fff;margin-bottom:6px">💬 네이버 톡톡 상담</div>
    <p style="font-size:16px;color:rgba(255,255,255,0.85);margin:0">궁금한 점이 있으시면 네이버 톡톡으로 편하게 문의해주세요!</p>
    <div style="display:inline-block;background:rgba(255,255,255,0.25);border:1px solid rgba(255,255,255,0.4);border-radius:20px;padding:6px 20px;margin-top:12px;font-size:14px;font-weight:600;color:#fff">스토어 채팅에서 "톡톡 문의" 클릭</div>
  </div>
  <div style="background:#F5F8FC;border-radius:14px;padding:24px 28px;margin-bottom:20px;border:1px solid #D6E4F0">
    <div style="font-size:20px;font-weight:700;color:#232F3E;margin-bottom:10px">
      <span style="color:#FF9900;font-weight:800;margin-right:8px">amazon</span> 🌍 해외 상품 안내
    </div>
    <p style="font-size:18px;color:#555;line-height:1.8;margin:0">
      미국 등 해외에서 판매하는 상품으로 <span style="color:#D94040;font-weight:600">사이즈, 사용 연령, 언어, AS</span> 등의 기준이 국내 제품과 다를 수 있습니다.
    </p>
  </div>
  <div style="background:#F0FAF5;border-radius:14px;padding:24px 28px;margin-bottom:20px;border:1px solid #B8E6D0">
    <div style="font-size:20px;font-weight:700;color:#232F3E;margin-bottom:10px">
      <span style="color:#FF9900;font-weight:800;margin-right:8px">amazon</span> 🔄 취소 및 반품
    </div>
    <div style="font-size:18px;color:#555;line-height:2;margin:0;padding-left:20px">
      · 결제 완료 후 바로 취소, <span style="color:#D94040;font-weight:600">배송 완료 후 7일 이내</span> 반품 가능<br>
      · 이후 고객센터를 통해 배송 완료 기준 <span style="font-weight:600">최대 30일 이전</span>까지 반품 신청이 가능합니다.<br>
      · 상품에 하자가 있는 경우 아마존 한국 고객센터(<span style="color:#FF9900;font-weight:700">1566-7171</span>/유료)로 연락주세요.
    </div>
    <div style="background:#E8F5E9;border-radius:10px;padding:14px 18px;margin-top:14px;font-size:16px;color:#666">
      ℹ️ 배송 규격에 따라서 상품이 분리 배송될 수 있습니다. 분리 배송된 상품을 고객 사유로 반품할 경우, 박스 별 반품 배송비가 부과될 수 있습니다.
    </div>
  </div>
  <div style="background:#F9F9F9;border-radius:14px;padding:18px 28px;border:1px solid #E0E0E0">
    <p style="font-size:16px;color:#888;line-height:1.7;margin:0">
      <span style="color:#FF9900;font-weight:800;margin-right:6px">amazon</span>
      아마존 미국이 판매하는 상품에 대해서는 아마존 글로벌 스토어 판매 조건이 적용됩니다.
    </p>
  </div>
</div>
<div style="height:5px;background:linear-gradient(90deg,#FF9900,#232F3E)"></div>"""

PA_SECTIONS = ["auth", "shipping", "gallery", "amazon_notice", "notice"]


def _build_pa_html(image_urls: list[str]) -> str:
    """PA 전용 상세페이지 HTML 조립. 순서: 정품→배송→상품이미지→아마존안내→주의사항."""
    # 상품 이미지 태그 생성
    if image_urls:
        img_tags = "\n".join(
            f'<img src="{url}" style="width:100%;display:block" alt="상품 이미지">'
            for url in image_urls
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
    return "\n".join(parts)


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
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (html, sections_json, market, existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO detail_pages
               (product_id, sections, html_content, market, platform, status)
               VALUES (?,?,?,?,?,?)""",
            (product_id, sections_json, html, market, platform, "draft"),
        )
        return cur.lastrowid


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
    if len(title_ko) > 50:
        title_ko = seo_title[:50] if len(seo_title) <= 50 else seo_title[:47] + "..."
    seo_tags_list = seo_result.get("tags") or seo_result.get("keywords") or []
    seo_tags = json.dumps(seo_tags_list, ensure_ascii=False) if seo_tags_list else "[]"

    mapped_category = existing_cat if cat_result is None else (cat_result.get("mapped_category") or existing_cat)

    # 4. HTML 생성 (PA 전용 템플릿)
    image_urls = img_result.get("local_urls") or []
    html = _build_pa_html(image_urls)

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
    """현재 실행 중인 job 조회 (running 또는 pending)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM batch_jobs WHERE status IN ('pending','running') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


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
