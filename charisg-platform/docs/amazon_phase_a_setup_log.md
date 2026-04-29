# Amazon Seller Phase A 셋업 진행 로그

> **최종 갱신**: 2026-04-15
> **Seller 계정**: CharisGlobal (한국 개인사업자, 미국 단일 마켓플레이스)
> **대표자**: Wongbi Ha
> **전략 단계**: Phase A (수동 MVP 운영)

---

## Phase 전략 요약

| Phase | 트리거 | 범위 |
|---|---|---|
| **Phase A (현재)** | 지금 ~ 1개월 | 수동 MVP 운영, 5~10개 상품으로 수요 검증 |
| Phase B | 주 10주문+ OR 상품 15개+ | SP-API 자동화 core 3개: Orders, Inventory, Shipment Confirmation |
| Phase C | 월 30주문+ OR 월매출 $5k+ | Listings, Pricing, Reports API 추가 |

**이유**: 초기 5~10개 상품은 수동 운영이 더 빠르고 실수 검증 가능. Amazon 이 새 셀러에게 첫 30일 판매 한도·카테고리 제한을 걸어두므로 자동화 의미 적음. 수동 리스팅으로 실제 Amazon 필드 스키마를 파악한 뒤 SP-API 연동에 반영.

---

## ✅ 완료 항목 (2026-04-14 ~ 2026-04-15)

### 1. Amazon 셀러 계정 승인
- 계정 타입: **Business → Sole Proprietor (개인사업자)**
- 법적 구조: Korean Sole Proprietorship
- 마켓플레이스: **United States only**
- Account Health: Healthy

### 2. Deposit Method 등록
- **Wise Multi-currency USD Account** (Personal)
- Partner bank: **Column Bank** (via Wise)
- Account holder: 셀러 명의와 일치 (Wongbi Ha)
- Status: Set as default for Amazon.com
- 민감 정보(계좌번호·Routing): 로컬 메모리 `project_charisg_platform.md` 참조

**중요 교훈**:
- 개인사업자 = Individual 분류이므로 Wise **Personal 계좌**로 충분
- Amazon 이 자동으로 Column Bank 를 third-party 로 오판해 거부 가능 → 거부 시 Seller Central 케이스 오픈

### 3. Tax Interview (W-8BEN)
- Classification: **Individual** (Sole Proprietor)
- Country of citizenship: **Republic of Korea**
- Tax treaty benefits: **Yes** (Korea-US treaty, **0% withholding**)
- TIN type: Foreign TIN
- Beneficial Owner Name: **개인 영문명 (Wongbi Ha)** — **NOT** trade name "CharisG"
- 주소: 사업장 또는 거주지 (영문)

**중요 교훈**:
- Amazon 이 Tax Interview 에서 사업체명을 pre-fill 해도 **Individual 선택 시 반드시 개인명으로 수정** 해야 함
- W-8BEN Line 1 = "Name of individual who is the beneficial owner"
- Deposit 계좌 명의와 Tax form 이름이 불일치하면 검증 실패 → 지급 보류

### 4. 마켓플레이스 단순화
- **Canada → Inactive**
- **Mexico → Inactive**
- **United States → Active (유일)**
- 경로: Seller Central → Settings → Account Info → Listings Status (또는 Vacation Settings)

**이유**:
- CJ Dropshipping US Warehouse 는 미국 국내 배송만 커버
- CA/MX 는 각각 GST/HST, RFC 등 별도 세무 등록 필요 → 복잡성 폭증
- 북미 통합 계정 기본 활성화를 수동 Inactive 로 정돈

### 5. Return Address (Medium 경고 — 무시)
- 현재: 한국 부천 주소 (셀러 가입 시 등록한 사업장)
- Amazon 이 Medium 레벨 "Review return address" 경고 유지
- **전략**: Return-less refund 로 실 반품 안 받음, 주소는 형식상 유지
- 경고 해제 시도: `Change address` 재저장 (효과 미확인) / Dismiss 버튼 / 그냥 무시 (판매 차단 아님)

---

## 🕐 대기 중

### SP-API Private Developer 심사
- 제출일: 2026-04-14
- 상태: 심사 중 (수 시간 ~ 1-2일 예상)
- 경로: Seller Central → Apps & Services → Develop Apps
- Developer Type: **Private Developer** (self-use only)

**제출 내용 요약**:
- 조직 이름: CharisG
- 조직 웹사이트: richspeakers.com
- 비제한 역할: **전체 체크** (Product Listing, Pricing, Orders, Inventory, Buyer Info, Reports 등)
- **제한됨 역할: 미체크** (D2C 배송, 세금 송장, 세금 송금, 전문 서비스)
  - ⚠️ 제한됨 체크 시 Public Developer 심사로 승급 → 수 주~수 개월
- 사용 사례: 영문 500자 이내 (Private Developer 선언, CJ Dropshipping 파트너 명시)
- 보안 제어: 전부 "예" (방화벽, MFA, HTTPS, 자격증명 안전 보관 등)

**승인 후 획득**:
- LWA Client ID (`amzn1.application-oa2-client.xxxx`)
- LWA Client Secret
- Refresh Token (self-authorize 후)
- Developer ID

→ 모두 `.env` 에 저장, `.gitignore` 필수. Phase B 착수 시 사용.

---

## ⏳ 남은 Phase A 셋업 작업

### 1. Shipping Template 생성 (15분)
**경로**: Seller Central → Settings → **Shipping Settings** → **Shipping Templates** → Create New Template

**값**:
- Template name: `CharisG US FBM Default`
- Handling Time: **2 business days** (여유 확보, Late Shipment Rate 방어)
- Shipping Service: Standard (3-5 business days)
- Shipping Region: **Continental US 48 states only**
- **제외**: Alaska, Hawaii, APO/FPO, Puerto Rico (국제 운송 복잡성 회피)
- 요금 모델: Free Shipping (가격에 포함) 또는 flat rate

### 2. Return Settings 설정 (10분)
**경로**: Settings → **Return Settings**

**Returnless Resolutions 룰 추가**:
- 조건 예: 상품가 < $25 → 자동 환불, 반품 요구 안 함
- 이유: 국제 반품 물류비(한국까지) > 상품가 → 환불이 합리적
- Amazon 정책상 허용되는 표준 전략

### 3. 스코어링 재실행 + 첫 후보 추출 (30분)
- `POST /api/ds/scoring/run?use_trends=true`
- 상위 10~20개 리뷰 (Matrix Group AA~AB 우선)
- **Hard Filter 8개 자동 통과 확인**:
  1. US 창고 보유 (`us_warehouse=True`)
  2. 마진 ≥ 25%
  3. 재고 ≥ 10
  4. 가격 $15~$70
  5. 무게 ≤ 907g (2 lbs)
  6. 이미지 ≥ 3장
  7. 차단 브랜드 아님
  8. 차단 카테고리 아님

### 4. 첫 리스팅 5~10건 수동 발행 (1~2시간)
- Amazon Seller Central → **Catalog → Add a Product**
- SP-API 승인 대기 중이므로 **수동 업로드** (Phase A 운영 원칙)
- 각 상품당 입력: ASIN 검색(이미 있으면 편승) / 새 SKU / 가격 / 재고 / Shipping Template 지정
- CJ Dropshipping 에 **Blind Dropshipping 설정 필수** (박스/송장에 CJ 로고 제거)
- Custom packing slip 에 CharisG 브랜딩 (CJ 유료 옵션)

---

## 📊 Amazon 계정 Health 메트릭 (지켜야 할 임계치)

| 메트릭 | 임계치 | 리스크 |
|---|---|---|
| Order Defect Rate (ODR) | < 1% | 재고 오판·고객 불만 |
| **Late Shipment Rate** | < 4% | Handling time 내 출하 확정 못할 때 |
| Pre-fulfillment Cancellation Rate | < 2.5% | CJ 재고 소진 미반영으로 취소 |
| **Valid Tracking Rate** | > 95% | 송장번호 누락 |

**임계 초과 → 계정 서스펜션 → 복구 수 주~수 개월**

---

## 🛡️ 드랍쉬핑 정책 준수 체크리스트

Amazon Dropshipping Policy 핵심:
- [x] Seller of record = **CharisGlobal** (본인)
- [ ] **Blind Dropshipping 설정** — CJ 패널에서 발신인/로고 제거
- [ ] **Custom Packing Slip** — CharisG 브랜드 (CJ 유료)
- [ ] 박스/송장에 **CJ 브랜딩 미노출**
- [ ] 영수증에 1688·Taobao 등 **타 리테일러 이름 금지**
- [x] Retail arbitrage dropshipping 금지 (Amazon/Walmart 에서 구매해 직송하는 모델 아님)

---

## 🏗️ 코드 자산 상태

### ✅ 이미 완료 (재사용 가능)
- CJ US Warehouse 수집 파이프라인 — `backend/dropshipping/services/cj_service.py`
- Hard Filter 8개 — `backend/dropshipping/services/scoring_service.py`
- 3축 스코어링 (Demand × Gap × Margin) — `backend/dropshipping/services/scoring_service.py:205-298`
- 리스팅 후보 DB (`dropshipping.db` `listings` 테이블)
- 리스팅 콘텐츠 편집 API — `backend/dropshipping/routers/ds_listings.py:36-61`

### ❌ 미구현 (Phase B 에서 개발)
- Amazon SP-API 연동 (Orders / Inventory / Shipment Confirmation)
- 자동 리스팅 업로드 발행
- 자동 재고 동기화 (CJ → Amazon)
- 자동 가격 관리

---

## 🔄 다른 컴퓨터에서 이어가기

1. 레포 클론:
   ```bash
   git clone https://github.com/SilverRain0716/CharisG-Platform.git
   cd CharisG-Platform
   git checkout main && git pull
   ```

2. 이 문서(`docs/amazon_phase_a_setup_log.md`) 열어서 **"⏳ 남은 Phase A 셋업 작업"** 섹션부터 진행

3. SP-API 승인 상태 확인:
   - Amazon 계정 이메일에서 "Developer Registration Approved" 메일 확인
   - 또는 Seller Central → Apps & Services → Develop Apps → 상태 확인

4. 승인됐으면 LWA Client ID/Secret 발급 받아 `.env` 에 저장:
   ```
   AMAZON_LWA_CLIENT_ID=amzn1.application-oa2-client.xxxxx
   AMAZON_LWA_CLIENT_SECRET=xxxxx
   AMAZON_REFRESH_TOKEN=xxxxx
   ```
   → `.gitignore` 필수 확인

5. Seller Central 수동 작업 이어서 진행 (Shipping Template → Return Settings → 첫 리스팅)

---

## 📚 관련 문서

- [dropshipping_ia_spec_v1.0.md](dropshipping_ia_spec_v1.0.md) — 드랍쉬핑 IA 설계서
- [dropshipping_dashboard_spec_v1.0.md](dropshipping_dashboard_spec_v1.0.md) — 드랍쉬핑 대시보드 기획
- [platform_architecture_v1.0.md](platform_architecture_v1.0.md) — 플랫폼 통합 아키텍처
- `../CLAUDE.md` — 플랫폼 아키텍처 규칙

---

**다음 세션 재개 명령어**: "아마존 드랍쉬핑 Phase A 이어서" 또는 "Amazon setup log 에서 남은 작업 계속"
