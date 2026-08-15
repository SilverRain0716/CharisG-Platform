"""약사법 / 식약처 hard block 키워드 — sourcing_promote 사전 차단용.

3계층 차단:
  - Tier 0 (DIET_DRUG / DIET_CLAIM): 비만 관련 신약 + 효능 강조형 (기존)
  - Tier 1 (TIER1_NARCOTIC):        마약류 / 향정신성 — 절대 불가
  - Tier 2 (TIER2_PHARMA):          의약품 원료 분류 — 수입 불가
  - Tier 3 (TIER3_UNAPPROVED):      식약처 미인정 원료 — 건기식 표방 판매 불가

모두 hard block (sourcing_promote 단계에서 DB 진입 차단).
"""
import re
from typing import Optional


# ── A. 의약품 / 처방 약물 (성분 직접 매칭) ─────────────────
# 국내 처방 의약품 또는 식약처 수입금지. 영업등록자라도 판매 불가.
DIET_DRUG_KEYWORDS: tuple[str, ...] = (
    # GLP-1 receptor agonist 계열 (당뇨/비만 처방약)
    "GLP-1", "GLP1",
    "세마글루타이드", "semaglutide",
    "오젬픽", "Ozempic",
    "위고비", "Wegovy",
    "삭센다", "Saxenda",
    "마운자로", "Mounjaro",
    "티르제파타이드", "tirzepatide",
    "리라글루타이드", "liraglutide",
    "라이벨서스", "Rybelsus",
    # 중추 식욕억제제 (마약류 또는 향정신성)
    "펜터민", "phentermine",
    "펜디메트라진", "phendimetrazine",
    "마진돌", "mazindol",
    "디에틸프로피온", "diethylpropion",
    "큐시미아", "qsymia",
    "콘트라브", "contrave",
    "토파맥스", "topamax", "topiramate",
    # 지방흡수억제제 (전문의약품)
    "제니칼", "Xenical",
    "오를리스타트", "orlistat",
    # GLP-1 우회 표현
    "체중감량 주사", "다이어트 주사", "위고비 주사",
)


# ── B. 효능 강조형 (성분과 무관하게 표시·광고 위반) ────────
# 보충제 카테고리도 "appetite suppressant" / "fat burner" 표현은
# 식약처 표시·광고 심의 미통과 시 약사법 위반 우려.
DIET_CLAIM_KEYWORDS: tuple[str, ...] = (
    # 영문
    "appetite suppressant", "appetite suppress",
    "fat burner", "fat burning pill",
    "diet pill", "diet pills",
    "weight loss pill", "weight loss pills",
    "slimming pill", "slimming pills",
    # 한글
    "식욕억제", "식욕 억제",
    "비만 치료", "비만치료제",
    "체중감량 약", "체중감량약",
    "다이어트 약", "다이어트약",
    "살빼는 약", "살 빼는 약",
)


# 영문 키워드는 단어 경계 (false positive 방지),
# 한글은 그대로 substring 매칭.
def _build_re(keywords: tuple[str, ...]) -> re.Pattern:
    parts = []
    for kw in keywords:
        if not kw:
            continue
        if re.search(r"[A-Za-z]", kw):
            parts.append(rf"\b{re.escape(kw)}\b")
        else:
            parts.append(re.escape(kw))
    return re.compile("|".join(parts), re.IGNORECASE)


# ── Tier 1: 마약류 / 향정신성 (절대 불가) ─────────────
TIER1_NARCOTIC: tuple[str, ...] = (
    "Kratom", "크라톰",
    "Ephedra", "에페드라", "ephedrine", "에페드린", "마황",
    "CBD", "Cannabidiol", "칸나비디올",
    "Androstenedione", "안드로스텐디온",
    "Kava Kava", "Kava", "카바", "카바카바",
    "Yohimbe", "Yohimbine", "요힘빈", "요힘베",
)


# ── Tier 2: 의약품 원료 분류 (수입 불가) ──────────────
TIER2_PHARMA: tuple[str, ...] = (
    "NAC", "N-Acetyl Cysteine", "N Acetyl Cysteine", "N 아세틸 시스테인", "N-아세틸시스테인",
    "Melatonin", "멜라토닌",
    "DHEA", "디에이치이에이",
    "Pregnenolone", "프레그네놀론",
    "5-HTP", "5HTP", "5 HTP",
    "Berberine", "베르베린",
    "Synephrine", "시네프린",
)


# ── Tier 3: 식약처 미인정 원료 (건기식 표방 판매 불가) ─
TIER3_UNAPPROVED: tuple[str, ...] = (
    "Ashwagandha", "아슈와간다", "아쉬와간다",
    "Maca", "마카",
    "Lion's Mane", "Lion Mane", "Lions Mane", "라이언메인", "사자갈기", "노루궁뎅이버섯",
    "Valerian", "발레리안", "쥐오줌풀",
    "St John", "St. John", "St Johns", "세인트존스워트", "서양고추나물", "성요한초",
    "Mullein", "멀레인",
    "Elderberry", "엘더베리",
    "Astragalus", "황기",
    "Echinacea", "에키네시아",
    "Tongkat Ali", "통캇알리",
    "Turkesterone", "터케스테론", "Ecdysterone", "엑디스테론",
    "Black Seed", "Nigella Sativa", "니젤라",
    "Comfrey", "컴프리",
)


# ── Tier 4: 한국 수입 완전 금지 (비-성분 — 무기/CITES/위험물 등) ─
TIER4_CUSTOMS: tuple[str, ...] = (
    # 마약류 (Tier 1 외 추가)
    "Marijuana", "Cannabis", "대마", "마리화나",
    "Cocaine", "코카인",
    "Opium", "아편",
    "MDMA", "Amphetamine", "암페타민",
    # 무기/폭발물
    "firearm", "총기", "모조 총기",
    "sword", "knife", "blade", "도검", "나이프", "칼날",
    "gunpowder", "explosive", "fireworks", "화약", "폭발물", "폭죽",
    "taser", "stun gun", "테이저", "전기충격기",
    # 음란물
    "porn", "pornographic", "음란",
    # CITES (멸종위기종)
    "ivory", "elephant tusk", "상아", "코끼리뼈",
    "tiger", "leopard", "호랑이가죽", "표범가죽",
    "crocodile leather", "alligator leather", "snake leather",
    "악어가죽", "도마뱀가죽",
    "coral jewelry", "산호장식", "shark fin", "상어지느러미", "샥스핀",
    # 위험물
    "radioactive", "방사성",
    "dry ice", "드라이아이스",
    "sodastream cylinder", "sodastream 실린더", "소다스트림 실린더",
    # 기타
    "live animal", "human remains", "ashes urn", "유골", "시신", "인체조직",
)


# ── Tier 5: malltail 추가 성분 (직구 통관 거부 사례 기반) ─────
TIER5_MALLTAIL: tuple[str, ...] = (
    # 의약품 (처방)
    "Sildenafil", "실데나필", "Viagra", "비아그라",
    "HCG", "human chorionic gonadotropin",
    # 소동물 유래 우려 (BSE)
    "beef extract", "beef tallow", "우피유래", "우유래",
    # 다이어트 / 식욕억제
    "Hoodia", "후디아", "Hoodia Gordonii",
    "Raspberry Ketones", "라즈베리 케톤", "라즈베리케톤",
    # 정력 / 호르몬 강화 — 식약처 미인정
    "Icariin", "이카린",
    "Horny Goat Weed", "호랑이풀", "호색초",
    "Muira Puama", "무이라푸아마",
    "Catuaba", "카투아바",
    "Tongkat Ali",  # 이미 Tier 3 에도 있음
    # 미인정 허브
    "Cat's Claw", "Cats Claw", "고양이발톱",
    "Cascara Sagrada", "카스카라",
    "Couch Grass", "카우치그라스", "쇠뜨기",
    "Buchu Leaf", "부추잎", "부추 잎",
    "Gymnema Sylvestre", "김네마", "기무네마",
    "Clubmoss", "Club Moss", "클럽모스",
    # 미인정 화학 / 미네랄
    "Vinpocetine", "빈포세틴",
    "Germanium", "게르마늄",
    "DIM", "Diindolylmethane", "디인돌릴메탄",
    "Cordyceps",  # 동충하초 — 식약처 미인정 시
    # 시트룰린 등 — 일부 dose-dependent (NO 부스터 우려)
    "L-Citrulline", "시트룰린", "씨트롤린",
)


_DRUG_RE = _build_re(DIET_DRUG_KEYWORDS)
_CLAIM_RE = _build_re(DIET_CLAIM_KEYWORDS)
_TIER1_RE = _build_re(TIER1_NARCOTIC)
_TIER2_RE = _build_re(TIER2_PHARMA)
_TIER3_RE = _build_re(TIER3_UNAPPROVED)
_TIER4_RE = _build_re(TIER4_CUSTOMS)
_TIER5_RE = _build_re(TIER5_MALLTAIL)


# ── Tier 6: 식약처 8조 1호 — 제품명에 병명 포함 (질병 예방·치료 효능 인식) ─
# 식품등의 표시·광고에 관한 법률 제8조 1호 위반 의심:
# "질병의 예방·치료에 효능이 있는 것으로 인식할 우려가 있는 표시 또는 광고"
# → 제품명에 병명만 들어가도 8조 1호 위반 적발 사례 다수.
TIER6_DISEASE_NAMES: tuple[str, ...] = (
    # 대사 질환
    "당뇨", "diabetes", "diabetic",
    "빈혈", "anemia", "anaemia",
    "고혈압", "저혈압", "hypertension",
    "고지혈증", "이상지질혈증", "고콜레스테롤",
    "비만", "obesity",
    # 심혈관
    "심장병", "뇌졸중", "동맥경화", "심혈관질환",
    "heart disease", "stroke", "arteriosclerosis", "atherosclerosis",
    # 뇌·정신
    "치매", "알츠하이머", "파킨슨",
    "dementia", "alzheimer", "parkinson",
    "우울증", "공황장애", "불면증", "수면장애",
    "depression", "insomnia",
    "두통", "편두통", "migraine",
    # 골관절
    "관절염", "류마티스", "골다공증", "골관절염",
    "arthritis", "rheumatoid", "osteoporosis",
    # 소화기
    "위염", "위궤양", "위장병", "장염", "대장염",
    "변비", "설사", "과민성대장",
    "gastritis", "ulcer", "constipation", "diarrhea",
    # 간·신장
    "간염", "간경화", "지방간", "신장병", "신부전",
    "hepatitis", "cirrhosis", "nephritis",
    # 비뇨생식기
    "방광염", "전립선", "발기부전",
    "prostatitis", "erectile dysfunction",
    # 호흡기
    "천식", "기관지염", "비염", "축농증",
    "결핵", "폐렴",
    "asthma", "bronchitis", "rhinitis", "sinusitis",
    "tuberculosis", "pneumonia",
    # 알레르기·피부
    "알레르기", "알러지", "아토피", "습진", "건선", "두드러기",
    "allergy", "allergies", "eczema", "psoriasis", "atopic", "atopy",
    "무좀", "발톱무좀", "주부습진",
    # 모발 (탈모는 약사법상 의약품 광고 영역)
    "탈모", "hair loss",
    # 안과
    "안구건조", "결막염", "녹내장", "백내장",
    "dry eye", "conjunctivitis", "glaucoma", "cataract",
    # 갑상선
    "갑상선", "갑상샘", "thyroid",
    # 효능 시사 (병명 인접)
    "콜레스테롤", "cholesterol",
    "혈당", "blood sugar",
    "혈압", "blood pressure",
)
_TIER6_RE = _build_re(TIER6_DISEASE_NAMES)


# Tier 6 false-positive 제외 — 동물용/측정기/물리치료 도구 등은
# 식품등 표시·광고법 8조 적용 대상 아님 (사료관리법 또는 의료기기법 별도).
# Tier 4 false-positive 제외 — 주방/공구/자동차 등 정상 카테고리
# 2026-05-20: "knife", "blade", "gun" 단순 매칭의 false positive 회피
_TIER4_EXEMPT_RE = re.compile(
    # 주방 칼
    r"kitchen knife|chef knife|paring knife|fruit knife|cooking knife|"
    r"steak knife|butter knife|cheese knife|bread knife|"
    r"주방 칼|과도|식칼|버터 나이프|치즈 나이프|빵 칼|"
    # 와이퍼/면도/톱날
    r"wiper blade|razor blade|scraper blade|saw blade|circular blade|"
    r"utility blade|box cutter|safety blade|"
    r"와이퍼|면도날|톱날|커터날|"
    # 글루건/네일건/스테이플건/스프레이건
    r"glue gun|hot glue|nail gun|staple gun|spray gun|paint gun|"
    r"caulking gun|grease gun|airbrush gun|stud gun|"
    r"글루건|네일건|스프레이건|"
    # 도검 일반 (장식/모형)
    r"katana display|sword display|sword stand|cosplay sword|"
    r"foam sword|toy sword|model sword|"
    r"플라스틱 칼|장난감 칼",
    re.IGNORECASE,
)


_TIER6_EXEMPT_CONTEXT_RE = re.compile(
    r"강아지|고양이|애견|애묘|반려|펫푸드|사료|"
    r"\bdog\b|\bcat\b|\bpet\b|\bpuppy\b|\bkitten\b|"
    r"측정기|혈압계|혈당계|체온계|모니터|진단|진단키트|검사키트|테스트지|"
    r"\bmonitor\b|\bgauge\b|\bmeter\b|test\s*strip|"
    r"퍼즐|puzzle|장난감|toy|"
    r"찜질|냉찜질|온찜질|아이마스크|안대|쿨링|핫팩|"
    r"빗|comb|brush|롤러|마사지기|massage|"
    r"수납|정리함|보냉|쿨러|케이스|organizer|storage|cooler|"
    r"\bbook\b|책|동화|도서|학습",
    re.IGNORECASE,
)


# ── Tier 7: 채널 정책 금지 (토스쇼핑/쿠팡/네이버 공통 — 한국 온라인 판매 제한) ─
# 2026-05-20: 토스쇼핑 가이드 + 한국 법령 기준으로 추가.
TIER7_CHANNEL_BANNED = (
    # 시력 보정 — 안경/콘택트렌즈 (의료기기 판매업 신고 필요)
    "prescription glasses", "prescription lens", "prescription eyewear",
    "도수 안경", "도수 렌즈", "시력 보정",
    "contact lens", "contact lenses", "콘택트렌즈", "콘택트 렌즈",
    "color contact", "circle lens", "미용 렌즈", "미용렌즈", "컬러 렌즈",
    # 초소형/몰래카메라 — Tier 5 보강
    "spy camera", "hidden camera", "spy cam", "covert camera",
    "초소형 카메라", "몰래 카메라", "몰래카메라",
    # 군복/유사 군복 (군복단속법)
    "military uniform", "combat uniform", "army uniform",
    "군복", "전투복", "유사 군복", "위장복",
    # 불법 어구 (수산자원관리법)
    "spear gun", "spearfishing gun", "harpoon gun",
    "작살총", "스피어건", "개불 펌프", "개불펌프",
    # 야생동물 포획 도구 (야생생물법)
    "wildlife trap", "animal snare",
    "올무", "포획덫",
    # 니코틴/담배 (담배사업법 — 온라인 판매 금지)
    "nicotine vape", "nicotine pouch", "nicotine cartridge",
    "vape juice", "vape pen", "e-liquid", "e-juice", "vape kit",
    "e-cigarette", "e cigarette", "electronic cigarette",
    "전자 담배", "전자담배", "니코틴", "vape mod",
    "rolling tobacco", "tobacco leaf", "연초 잎",
    # 주류 (조건부 외 모두 — 주세법)
    "whiskey", "vodka", "rum bottle", "scotch whisky", "bourbon whiskey",
    "위스키", "보드카", "양주", "스카치",
    # 총포/도검/화약 (총포도검법)
    "firearm", "handgun", "rifle scope", "pistol",
    "권총", "총포", "소총", "화약",
    "combat knife", "hunting knife", "switchblade", "butterfly knife",
    "도검", "사시미칼",
    # 살아있는 동물 (동물보호법)
    "live animal", "live pet sale", "live fish for sale",
    "강아지 분양", "고양이 분양", "생물 분양",
    # 혈액/헌혈증서 (혈액관리법)
    "blood donation card", "헌혈증",
    # 장물/분실물/문화재 (형법/문화재보호법)
    "stolen goods", "장물",
    # 위조품 (상표법)
    "replica gucci", "replica louis vuitton", "replica brand",
    "fake brand", "counterfeit",
    "이미테이션", "짝퉁",
    # 현금화 가능 — 금/다이아/상품권/가상화폐 (전자금융거래법/통신판매업)
    "gold bar", "gold bullion", "골드바", "순금 바",
    "loose diamond",
    "gift card amazon", "gift card itunes", "gift card google",
    "amazon gift", "itunes gift", "google play gift",
    "기프트카드", "상품권",
    # NFT/가상화폐 (특금법)
    "nft collectible", "nft art", "nft drop", "nft token", "non-fungible token",
    "cryptocurrency", "bitcoin coin", "ethereum coin",
    "가상 화폐", "암호 화폐", "NFT 컬렉션", "NFT 아트",
    # 사행성 — 랜덤박스 (사행행위법)
    "mystery box", "loot box", "random gachapon",
    "랜덤박스", "럭키박스", "복권",
    # 청소년 유해 (청소년보호법)
    "butane gas", "butane fuel", "lighter fluid",
    "부탄 가스", "라이터 가스",
)
_TIER7_RE = _build_re(TIER7_CHANNEL_BANNED)

# Tier 7 false-positive 제외 — 자동차/주방용/반려동물 등 정상 카테고리
_TIER7_EXEMPT_RE = re.compile(
    # 자동차 부품 (오일/연료/필터)
    r"automotive|car part|auto part|vehicle|engine oil|"
    r"자동차|차량용|연료 필터|엔진오일|"
    # 카메라 일반 (행동/포지셔닝)
    r"dashboard cam|dash cam|action camera|security camera|"
    r"webcam|webcamera|cctv|"
    r"대시캠|블랙박스|보안 카메라|cctv 카메라|"
    # 케이크/와인잔 (주류 자체 아님)
    r"wine glass|wine rack|wine opener|wine stopper|"
    r"와인 잔|와인 거치|와인 오프너|"
    # 칼 일반 (주방용/공예용)
    r"kitchen knife|chef knife|paring knife|fruit knife|"
    r"주방 칼|과도|식칼|"
    # 가짜 식물/장식
    r"fake plant|fake flower|fake fur|fake leather|"
    r"가짜 식물|인조 가죽|"
    # 카드 일반 (상품권 아님)
    r"\bcard holder\b|business card|credit card holder|"
    r"명함 지갑|카드 지갑|"
    # 토큰 (NFT 아님)
    r"coin holder|coin album|coin display|"
    r"동전 정리|동전 케이스",
    re.IGNORECASE,
)


# ── Tier 8: 음란/성인용품 (성인인증 채널만 가능 — 토스쇼핑 등 일반 채널 차단) ─
TIER8_ADULT = (
    "sex toy", "adult toy", "dildo", "vibrator",
    "anal plug", "butt plug", "fleshlight", "masturbator",
    "love doll", "real doll", "리얼 돌", "리얼돌",
    "성인용품", "자위 기구", "자위기구", "딜도", "바이브레이터",
    "lubricant sex", "personal lubricant",
    "성인용 게임", "어덜트 토이", "adult only",
    "hardcore porn",
    "음란물",
)
_TIER8_RE = _build_re(TIER8_ADULT)


# Tier 9 — KC 인증 의무 (구매대행 금지 또는 제한). 2026-05-21 추가.
# 근거: 전기용품 및 생활용품 안전관리법 + 어린이제품 안전특별법.
# 분류:
#   (9-a) 안전인증 대상 (구매대행 금지) — 전기충전기, 전기다리미, 전기온수기, 압력솥 등
#   (9-b) 안전확인/공급자적합성 대상 (구매대행 가능하나 KC 미보유 시 위험)
#   (9-c) 어린이제품 — 어린이제품 안전특별법 (별도 regime, 모두 KC 의무)
# 매칭 후보는 검사 후 차단. false positive 회피 위해 일부 EXEMPT 규칙 추가.
TIER9_KC_REQUIRED = (
    # 전기충전기 (안전인증, 구매대행 금지) — "충전기" 단독, EXEMPT 로 false positive 보정
    "충전기", "charger", "battery charger",
    "전기충전기", "PS4 충전기", "PS5 충전기", "무선 충전기", "wireless charger",
    # 전기다리미/청소기/가습기/전열기 (안전인증)
    "전기다리미", "전기청소기", "가습기", "전열기", "전기담요", "전기매트",
    "전기온수기", "전자레인지", "전기건조기", "전기마사지기",
    "압력냄비", "압력솥", "전기 압력", "압력 솥",
    # 이미용기기 (안전인증/안전확인) — 모발관리기/면도/이발
    "고데기", "드라이기", "면도기", "제모기", "전동칫솔", "구강세정",
    "hair dryer", "shaver", "epilator", "electric toothbrush",
    # 헬스기구/스포츠 (안전확인 생활용품)
    "헬스기구", "덤벨", "바벨", "스케이트보드", "스노보드", "스키용구",
    "이륜자전거", "롤러스케이트", "킥보드", "인라인롤러스케이트",
    "skateboard", "snowboard", "ski equipment",
    # 안전모/헬멧
    "승차용 안전모", "운동용 안전모", "스포츠용 헬멧",
    # 안경/선글라스/물안경 (안전기준 준수)
    "안경테", "선글라스", "물안경", "sunglasses", "eyeglasses", "swim goggles",
    # 침대매트리스 (안전기준 준수)
    "매트리스", "mattress",
    # 텐트/우산/양산
    "텐트", "우산", "양산", "tent", "umbrella",
    # 가전 (안전확인) — TV/모니터/프린터/노트북/오디오/스피커/프로젝터
    # 키워드가 너무 광범위 — EXEMPT 룰로 보호
    "텔레비전", "디지털TV", "스마트 TV", "IPTV",
    "노트북", "노트북컴퓨터", "태블릿", "테블릿 PC", "디스크플레이어",
    "프로젝터", "오디오시스템", "스피커", "speaker",
    "모니터", "monitor", "프린터", "printer", "복사기",
    "laptop", "notebook computer", "tablet PC",
    # 게임기/콘솔/조이패드 (공급자 적합성)
    "비디오게임기구", "게임 콘솔", "PlayStation 콘솔", "Xbox 콘솔",
    # 헤드폰/이어폰 — 무선만 KC 대상
    "무선 이어폰", "무선 헤드폰", "블루투스 이어폰", "블루투스 헤드폰",
    "wireless earbud", "bluetooth headphone",
    # 어린이제품 안전특별법 — 명확 연령/유아/토이
    "유아용", "영유아", "어린이용", "토들러용",
    "Schleich 피규어", "Melissa & Doug",
    # 컴퓨터 전원공급장치 (안전확인)
    "컴퓨터 전원공급장치",
    # 가스라이터/비비탄총 (생활용품 안전인증)
    "가스라이터", "부탄가스 라이터", "비비탄총",
    # 전기차 충전기 (안전확인)
    "전기차 충전기", "EV charger",
    # 사우나/관상/애완용 전기기기
    "전기 사우나", "관상용 어항", "애완용 히터",
)
_TIER9_RE = _build_re(TIER9_KC_REQUIRED)

# Tier 9 면제 — 광범위 키워드의 false positive 회피.
# 예: "TV 거치대", "노트북 케이스/스탠드", "자동차용 스피커" 등 액세서리.
_TIER9_EXEMPT_RE = re.compile(
    r"(거치대|마운트|stand|case\b|케이스|스탠드|holder|쿠션|커버|cover|"
    r"파우치|pouch|보호필름|screen protector|쿨링패드|cooling pad|"
    r"자동차용 스피커|car speaker|차량용 스피커|"
    r"매트리스 커버|mattress cover|매트리스 패드 커버|"
    r"우산꽂이|umbrella stand|텐트 폴|tent pole|"
    r"헬멧 인너|helmet liner|"
    r"안경 닦이|안경집|안경 케이스|선글라스 케이스|"
    r"드라이브|drive |hard drive|usb drive|external drive|"
    r"청소기 필터|cleaner filter|진공청소기 부속|"
    r"가습기 필터|humidifier filter|"
    r"노트북 가방|laptop bag|laptop case|laptop sleeve|"
    r"노트북 스킨|laptop skin|"
    r"모니터 받침|monitor riser|모니터 암|monitor arm|"
    r"프린터 잉크|printer ink|프린터 토너|printer toner|프린터 용지|printer paper|"
    r"충전 케이블|충전케이블|charging cable|usb 케이블|usb cable|"
    r"충전 패드|charging pad|충전기 거치|"
    r"스피커 케이블|speaker cable|스피커 스탠드|speaker stand|"
    r"매트리스 토퍼|mattress topper|mattress protector|"
    r"우산 살|우산살|"
    r"마사지 오일|massage oil|마사지 크림|massage cream)",
    flags=re.IGNORECASE,
)

# ── Tier 10: 화장품/건강기능식품 임시 STOP (2026-07-16 KST 도입) ────────
# 사장님 계획: Gemini 월 한도 복구 후 8월에 self_made(~3,800) KIPRIS 조회+별도 마이그.
# 현재는 실수 등록(예: Evian 페이셜 스프레이 5건, 214251~214255) 방지 목적.
# 8월 재개 시 이 Tier만 주석/제거하면 됨.
TIER10_COSMETIC_SUPPLEMENT: tuple[str, ...] = (
    # ── 화장품: 페이셜/스킨케어 명확 지표 ────
    "페이셜 스프레이", "페이셜 미스트", "페이셜 크림", "페이셜 로션",
    "페이셜 토너", "페이셜 클렌저", "페이셜 세럼", "페이셜 에센스",
    "페이셜 오일", "페이셜 마스크",
    "facial spray", "facial mist", "facial cream", "facial lotion",
    "facial toner", "facial cleanser", "facial serum", "facial essence",
    "face mist", "face toner", "face serum",
    "스킨케어", "skincare", "skin care",
    "미네랄 워터 미스트", "미네랄 미스트", "mineral water mist",
    "스킨토너", "스킨로션", "스킨크림", "스킨에센스", "스킨앰플",
    "스킨미스트", "스킨세럼",
    "화장수", "미백크림", "주름크림", "안티에이징 크림",
    # ── 스킨케어 성분 ────
    "나이아신아마이드", "niacinamide",
    "레티놀", "retinol",
    "히알루론산", "hyaluronic acid",
    "아데노신", "adenosine",
    # ── 클렌징 ────
    "클렌징 오일", "클렌징 폼", "클렌징 워터", "클렌징 젤", "클렌징 밤",
    "cleansing oil", "cleansing foam", "cleansing water",
    "각질제거", "exfoliator", "필링젤", "peeling gel",
    # ── 마스크팩 ────
    "마스크팩", "sheet mask", "시트마스크", "시트 마스크",
    "슬리핑 마스크", "sleeping mask",
    # ── 립케어/립메이크업 ────
    "립스틱", "lipstick", "립글로스", "lip gloss", "립밤", "lip balm",
    "립케어", "립스크럽", "립라이너", "lip liner", "립틴트", "lip tint",
    # ── 아이 메이크업 ────
    "아이섀도우", "eye shadow", "eyeshadow",
    "마스카라", "mascara",
    "아이라이너", "eyeliner",
    "brow pencil", "eyebrow pencil",
    # ── 페이스 메이크업 ────
    "BB크림", "BB cream", "CC크림", "CC cream", "DD크림", "DD cream",
    "파운데이션 메이크업", "액체 파운데이션", "쿠션 파운데이션", "파운데이션 쿠션",
    "foundation makeup",
    "컨실러", "concealer",
    "볼터치", "블러셔", "blusher",
    "컨투어링", "contouring", "쉐이딩 팔레트",
    "하이라이터 팔레트", "highlighter palette",
    "메이크업 프라이머", "makeup primer",
    # ── 자외선차단 ────
    "자외선차단제", "선크림", "sun cream", "선블록", "sunblock",
    "선스틱", "sun stick", "sunscreen",
    # ── 세럼/앰플 (거의 항상 스킨케어) ────
    "세럼", "serum",
    "앰플", "ampoule",
    # ── 건강기능식품 ────
    "종합비타민", "multivitamin",
    "비타민 D", "비타민D", "vitamin D",
    "비타민 C", "비타민C", "vitamin C",
    "비타민 E", "비타민E", "vitamin E",
    "비타민 B", "비타민B", "vitamin B",
    "비타민 A", "비타민A", "vitamin A",
    "비타민 K", "비타민K", "vitamin K",
    "오메가3", "omega-3", "omega 3", "omega3",
    "fish oil", "피시오일", "어유",
    "프로바이오틱스", "probiotic", "probiotics",
    "코엔자임Q10", "coenzyme Q10", "CoQ10", "coq10",
    "루테인", "lutein",
    "밀크씨슬", "milk thistle",
    "크릴오일", "krill oil",
    "BCAA",
    "글루코사민", "glucosamine",
    "콘드로이틴", "chondroitin",
    "MSM", "methylsulfonylmethane",
    "프리바이오틱스", "prebiotic",
    "유청단백질", "whey protein",
    "폴리코사놀", "policosanol",
    "홍삼 캡슐", "홍삼 정", "홍삼 파우더", "홍삼 스틱",
    "콜라겐 파우더", "collagen powder", "콜라겐 드링크", "collagen drink",
    "collagen supplement",
)
_TIER10_RE = _build_re(TIER10_COSMETIC_SUPPLEMENT)

# Tier 10 면제 — 화장품/건기식 아닌 흔한 오탐 회피.
_TIER10_EXEMPT_RE = re.compile(
    # 크림 컨텍스트 (식품)
    r"아이스크림|ice ?cream|크림빵|크림 스프|크림 치즈|cream cheese|"
    r"크림 파스타|cream pasta|코코아 크림|cocoa cream|커피 크림|coffee cream|"
    r"휘핑 크림|whipping cream|whipped cream|크림 소스|"
    # 미스트 non-cosmetic (조리/살균/선풍기 등)
    r"오일 미스트|조리 미스트|요리용 미스트|살균 미스트|소독 미스트|"
    r"mist sprayer|spray bottle|mist fan|mister\s|헤어 미스트 분사기|"
    r"미스트 팬|미스트 선풍기|"
    # 마스크 non-cosmetic (호흡보호구/방한/스포츠 등)
    r"KF마스크|KF ?mask|방진마스크|방독마스크|스포츠 ?마스크|3M 마스크|"
    r"산업용 마스크|dust mask|respirator|N95|KN95|face shield|"
    r"방수 마스크|스노클 마스크|snorkel mask|잠수 마스크|"
    r"수면 마스크|sleep mask cover|눈가리개 마스크|"
    # 앰플 non-cosmetic (유리/의료용)
    r"유리 앰플|의료용 앰플|glass ampoule|ampoule holder|ampoule glass|"
    # 홍삼 non-cosmetic (전통차/사탕)
    r"홍삼차|ginseng tea|홍삼 젤리|홍삼 사탕|홍삼 캔디|"
    # 노트북/전자 스킨(이미 Tier9 EXEMPT지만 안전망)
    r"노트북 스킨|laptop skin|스티커 스킨|데코 스킨|"
    # 프라이머 non-cosmetic (도장)
    r"페인트 프라이머|도장 프라이머|벽 프라이머|primer paint|"
    # 파운데이션 non-cosmetic (건축은 거의 안 팔지만 대비)
    r"건축 파운데이션|파운데이션 매트\s|"
    # 하이라이터 non-cosmetic (형광펜)
    r"형광펜|highlighter pen|highlighter marker|"
    # 비타민 non-supplement (일반식품 — 사장님이 별도 판단 원할 수 있음)
    r"비타민 워터|vitamin water|비타민 캔디|vitamin candy|비타민 젤리|vitamin jelly|"
    # 프로틴 non-supplement
    r"프로틴 바|protein bar|프로틴 쿠키|protein cookie|프로틴 아이스크림|"
    r"프로틴 시리얼|protein cereal",
    flags=re.IGNORECASE,
)


def is_banned_diet_product(title_en: str, title_ko: str = "") -> Optional[str]:
    """약사법 / 식약처 hard block 대상이면 매칭 키워드 반환, 통과면 None.

    검사 순서: Tier 1 → Tier 2 → Tier 3 → 기존 의약품 → 효능표현 → Tier 7-8 채널 정책.
    title_en / title_ko 둘 다 검사 (한쪽만 있어도 OK).
    """
    haystack = f"{title_ko or ''} {title_en or ''}"
    if not haystack.strip():
        return None
    m = _TIER1_RE.search(haystack)
    if m:
        return f"마약류:{m.group(0)}"
    m = _TIER2_RE.search(haystack)
    if m:
        return f"의약품원료:{m.group(0)}"
    m = _TIER3_RE.search(haystack)
    if m:
        return f"미인정원료:{m.group(0)}"
    m = _TIER4_RE.search(haystack)
    if m and not _TIER4_EXEMPT_RE.search(haystack):
        return f"수입금지:{m.group(0)}"
    m = _TIER5_RE.search(haystack)
    if m:
        return f"통관거부:{m.group(0)}"
    m = _DRUG_RE.search(haystack)
    if m:
        return f"의약품:{m.group(0)}"
    m = _CLAIM_RE.search(haystack)
    if m:
        return f"효능표현:{m.group(0)}"
    # Tier 6 — 식약처 8조 1호 (병명 포함). 동물/측정기/물리치료/책 등 컨텍스트는 면제.
    m = _TIER6_RE.search(haystack)
    if m and not _TIER6_EXEMPT_CONTEXT_RE.search(haystack):
        return f"식약처8조1호:{m.group(0)}"
    # Tier 7 — 채널 정책 금지 (한국 온라인 판매 제한). 토스쇼핑/쿠팡/네이버 공통.
    m = _TIER7_RE.search(haystack)
    if m and not _TIER7_EXEMPT_RE.search(haystack):
        return f"채널정책:{m.group(0)}"
    # Tier 8 — 음란/성인용품 (성인인증 별도 채널만 가능)
    m = _TIER8_RE.search(haystack)
    if m:
        return f"성인용품:{m.group(0)}"
    # Tier 9 — KC 인증 의무 (전기/생활용품 안전관리법 + 어린이제품 안전특별법).
    # EXEMPT (액세서리/케이스/거치대 등) 룰 적용 후 차단.
    m = _TIER9_RE.search(haystack)
    if m and not _TIER9_EXEMPT_RE.search(haystack):
        return f"KC인증의무:{m.group(0)}"
    # Tier 10 — 화장품/건기식 임시 STOP (2026-08 Gemini 복구 후 KIPRIS 처리 예정).
    m = _TIER10_RE.search(haystack)
    if m and not _TIER10_EXEMPT_RE.search(haystack):
        return f"화장품건기식임시:{m.group(0)}"
    return None
