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
    "live animal", "human remains", "ashes urn", "유골", "인체",
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


def is_banned_diet_product(title_en: str, title_ko: str = "") -> Optional[str]:
    """약사법 / 식약처 hard block 대상이면 매칭 키워드 반환, 통과면 None.

    검사 순서: Tier 1 → Tier 2 → Tier 3 → 기존 의약품 → 효능표현.
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
    if m:
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
    return None
