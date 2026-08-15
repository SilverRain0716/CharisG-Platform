"""
clean_policy.py — 네이버/쿠팡 클린 위반 방지 공용 정책 모듈.

3중 게이트 (sourcing → ai → upload) 의 단일 진입점.
모든 금지 키워드/효능 표현/카테고리/속성 정책은 이 파일에서만 관리한다.

배경 (2026-05-04):
  스마트스토어 클린위반 157건 적발 (중복 150 + 허위과대광고 3 + 취급불가 4).
  기존 coupang_lister.py 의 BANNED_INGREDIENT_KEYWORDS / EFFICACY_CLAIM_PATTERNS 를
  공용 모듈로 추출하고, 네이버 + 소싱 + AI 후처리에도 동일 정책 적용.

사용 예:
    from backend.purchase.services import clean_policy

    # 입구 (sourcing_promote)
    blocked, kw = clean_policy.check_prohibited_ingredients(title_en, title_ko)
    if blocked:
        clean_policy.log_violation(stage='sourcing', violation_type='prohibited_ingredient',
                                    matched_keyword=kw, asin=asin, action='blocked')
        continue

    # AI 후처리 (ai_processor) — 건강식품만
    if clean_policy.is_health_food_category(category_path):
        title_ko = clean_policy.sanitize_efficacy_claims(title_ko)

    # 업로드 (smartstore_lister / coupang_lister)
    dup, info = clean_policy.check_duplicate_asin(asin, channel='smartstore', exclude_product_id=pid)
    if dup:
        return {'ok': False, 'skip': True, 'error': f'중복 ASIN ({info["channel_product_id"]})'}
"""
from __future__ import annotations

import logging
import json
import re
from typing import Optional

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 1. 금지 성분 (Prohibited Ingredients) — Hard Block
# ═══════════════════════════════════════════════════════════
# 매칭 시 등록 차단. 영업등록 / 식약처 신고와 무관하게 한국 수입금지 또는 의약품 분류.
# Tier 1: 마약류 / 향정신성
# Tier 2: 의약품 원료 (한국 처방 의약품)
# Tier 3: 식약처 미인정 원료
# Tier 4: 한국 수입 완전 금지 (비-성분)
# Tier 5: malltail 통관 거부 사례
PROHIBITED_INGREDIENTS = (
    # ── Tier 1: 마약류 / 향정신성 ──
    "Kratom", "크라톰",
    "Ephedra", "에페드라", "ephedrine", "에페드린", "마황",
    "CBD", "Cannabidiol", "칸나비디올",
    "THC", "tetrahydrocannabinol",
    "Androstenedione", "안드로스텐디온",
    "Kava Kava", "Kava", "카바", "카바카바",
    "Yohimbe", "Yohimbine", "요힘빈", "요힘베",
    # ── Tier 2: 의약품 원료 ──
    "NAC", "N-Acetyl Cysteine", "N Acetyl Cysteine", "N 아세틸 시스테인", "N-아세틸시스테인",
    "Melatonin", "melatonin", "멜라토닌",
    "DHEA", "디에이치이에이",
    "Pregnenolone", "pregnenolone", "프레그네놀론",
    "5-HTP", "5HTP", "5 HTP", "5-htp",
    "Berberine", "베르베린",
    "Synephrine", "시네프린",
    "PABA", "파바", "Para-Aminobenzoic Acid",
    # ── Tier 3: 식약처 미인정 원료 ──
    "Shilajit", "실라짓", "쉬라짓",
    # ── 2026-05-04 쿠팡 DENIED 분석 기반 추가 ──
    "Zicam", "동종요법", "Homeopathic", "homeopathic",
    "Slippery Elm", "슬리퍼리엘름", "슬리퍼리 엘름",
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
    "Comfrey", "comfrey", "컴프리",
    "PQQ", "Pyrroloquinoline",
    # ── 비만 약물 ──
    "시부트라민", "sibutramine",
    "펜플루라민", "fenfluramine",
    "프로게스테론", "progesterone",
    # ── 기타 국내 미허용 ──
    "콜로이드은", "colloidal silver",
    # ── Tier 4: 한국 수입 완전 금지 (비-성분) ──
    # 2026-05-22 오탐 수정: 단어경계 매칭이 일반 상품을 차단하던 일반어 제거/축소.
    #   - "explosive" 제거: 피트니스 "explosive power/strength" 오탐 (실폭발물은 gunpowder/fireworks/폭발물로 커버)
    #   - "blade" 제거: "shoulder blade" 등 오탐 (실도검은 sword/도검으로 커버)
    #   - "ivory" 제거: 색상 ivory 오탐 (실상아는 elephant tusk/상아/코끼리뼈로 커버)
    #   - "tiger"/"leopard" → fur/skin 다단어: "Tiger Muay Thai"·"leopard print" 제외, 실모피만 차단
    "Marijuana", "Cannabis", "대마", "마리화나",
    "Cocaine", "코카인",
    "Opium", "아편",
    "MDMA", "Amphetamine", "암페타민",
    "firearm", "총기", "모조 총기",
    "sword", "도검",
    "gunpowder", "fireworks", "화약", "폭발물", "폭죽",
    "taser", "stun gun", "테이저", "전기충격기",
    "porn", "pornographic", "음란",
    "elephant tusk", "상아", "코끼리뼈",
    "tiger fur", "tiger skin", "tiger pelt", "leopard fur", "leopard skin", "호랑이가죽", "표범가죽",
    "crocodile leather", "alligator leather", "snake leather",
    "악어가죽", "도마뱀가죽",
    "coral jewelry", "산호장식", "shark fin", "상어지느러미", "샥스핀",
    "radioactive", "방사성",
    "dry ice", "드라이아이스",
    "sodastream cylinder", "소다스트림 실린더",
    # ── 가연성/위험물 (항공 해외배송 금지 — 부탄 토치·라이터·연료) 2026-06-29 ──
    "토치 라이터", "토치라이터", "가스 토치", "가스토치", "용접 토치", "용접토치",
    "블로우 토치", "블로우토치", "부탄 토치", "부탄토치", "부탄가스", "부탄 가스", "지포 라이터",
    "라이터 기름", "라이터 연료", "라이터 가스", "오일 라이터", "방풍 라이터",
    "torch lighter", "blow torch", "blowtorch", "butane", "lighter fluid",
    "lighter fuel", "zippo", "windproof lighter", "refillable lighter", "jet lighter",
    "live animal", "human remains", "ashes urn", "유골", "시신", "인체조직",
    # ── Tier 5: malltail 통관 거부 사례 ──
    "Sildenafil", "실데나필", "Viagra", "비아그라",
    "HCG", "human chorionic gonadotropin",
    "beef extract", "beef tallow", "우피유래", "우유래",
    # ── 2026-05-07 식약처 BBL Gummies 적발 (Hydrolyzed Bovine Hide Collagen Peptides) ──
    "Bovine Hide", "Hydrolyzed Bovine", "Bovine Collagen Peptide",
    "Bovine Collagen Peptides", "Cow Hide Collagen", "Cattle Hide",
    "우피", "소가죽 콜라겐", "쇠가죽 콜라겐",
    "Hoodia", "후디아", "Hoodia Gordonii",
    "Raspberry Ketones", "라즈베리 케톤", "라즈베리케톤",
    "Icariin", "이카린",
    "Horny Goat Weed", "호랑이풀", "호색초",
    "Muira Puama", "무이라푸아마",
    "Catuaba", "카투아바",
    "Cat's Claw", "Cats Claw", "고양이발톱",
    "Cascara Sagrada", "카스카라",
    "Couch Grass", "카우치그라스",
    "Buchu Leaf", "부추잎",
    "Gymnema Sylvestre", "김네마", "기무네마",
    "Clubmoss", "Club Moss", "클럽모스",
    "Vinpocetine", "빈포세틴",
    "Germanium", "게르마늄",
    "DIM", "Diindolylmethane", "디인돌릴메탄",
    "Cordyceps",
    "L-Citrulline", "시트룰린", "씨트롤린",
    # ── Tier 6: 항공 운송 제한 / 배터리 (UN 38.3) ──
    # 리튬 배터리/배터리 내장 제품은 국제 항공 운송 제한 → 통관 자체 거부
    "lithium battery", "lithium-ion", "li-ion battery",
    "리튬 배터리", "리튬배터리", "리튬이온", "리튬-이온",
    "rechargeable battery", "충전식 배터리", "충전배터리",
    "power bank", "powerbank", "보조배터리",
    "battery pack", "battery-powered", "battery operated",
    "lithium polymer", "Li-Po", "리튬폴리머", "리튬 폴리머",
    # ── Tier 7: IP 라이선스 (저작권/상표권 위반 방지) ──
    # 2026-05-07 쿠팡 Boosters Copyright 신고 + 향후 신고 사례 기반.
    # 글로벌 라이선스 IP — 캐릭터/만화/영화/게임/책 캐릭터 굿즈.
    # substring 매칭이라 일반 명사와 충돌하는 키워드는 의도적으로 다단어 형태만 사용.
    # Disney 산하
    "Disney", "디즈니", "Disney Princess", "Mickey Mouse", "Minnie Mouse",
    "미키마우스", "미니마우스", "Disney Frozen", "겨울왕국", "Toy Story",
    "토이스토리", "Buzz Lightyear", "Lightning McQueen", "Lion King",
    "라이온킹", "Little Mermaid", "Disney Aladdin", "Disney Tangled",
    "Disney Moana", "모아나", "Disney Encanto", "Inside Out Disney",
    "Mickey Mouse Clubhouse", "Lilo & Stitch", "Pixar", "픽사", "Disney Cars",
    # Marvel/DC
    "Marvel", "마블", "Avengers", "어벤져스", "어벤저스",
    "Spider-Man", "Spiderman", "Spider Man", "스파이더맨",
    "Iron Man Marvel", "아이언맨", "Captain America", "캡틴아메리카",
    "Incredible Hulk", "Thor Marvel", "Black Panther Marvel", "X-Men",
    "Batman", "배트맨", "Superman", "슈퍼맨", "Wonder Woman", "Justice League",
    "DC Comics",
    # Star Wars / Harry Potter / LOTR
    "Star Wars", "스타워즈", "Mandalorian", "Baby Yoda", "Grogu",
    "Harry Potter", "해리포터", "Hogwarts", "Hermione",
    "Lord of the Rings", "Hobbit",
    # Nintendo / Pokemon
    "Pokemon", "Pokémon", "포켓몬", "Pikachu", "피카츄", "Charizard",
    "Pokemon Card", "Pokémon Card", "포켓몬 카드",
    "Super Mario", "슈퍼마리오", "Nintendo Mario", "Legend of Zelda", "Zelda Nintendo",
    "Nintendo Switch", "닌텐도", "Animal Crossing", "Smash Bros", "Splatoon",
    # 카드 게임 / TCG
    "Trading Card Game", "Booster Pack", "Booster Box",
    "Yu-Gi-Oh", "YuGiOh", "유희왕", "Magic the Gathering",
    "Lorcana", "Flesh and Blood", "One Piece Card",
    # 일본 IP
    "Hello Kitty", "헬로키티", "Sanrio", "산리오",
    "My Melody", "마이멜로디", "Kuromi", "Cinnamoroll", "시나모롤",
    "Doraemon", "도라에몽", "Naruto", "나루토", "Dragon Ball", "드래곤볼",
    "Demon Slayer", "귀멸의 칼날", "My Hero Academia",
    "Studio Ghibli", "Totoro", "토토로", "지브리",
    "Sailor Moon", "세일러문", "Anpanman", "호빵맨",
    # 어린이 캐릭터
    "Sesame Street", "Cookie Monster", "Big Bird",
    "Snoopy", "스누피", "Charlie Brown",
    "Bluey", "Paw Patrol", "퍼피구조대", "Peppa Pig", "페파피그",
    "Cocomelon", "코코멜론", "Daniel Tiger", "Dora the Explorer",
    "Curious George", "Thomas the Tank Engine", "Thomas & Friends",
    "Sonic the Hedgehog", "Garfield", "Powerpuff Girls", "Care Bears",
    "Minions", "미니언즈", "Despicable Me", "Shrek", "슈렉",
    "Madagascar Dreamworks", "Kung Fu Panda", "Jurassic World", "Jurassic Park",
    "Tom and Jerry", "톰과 제리", "Looney Tunes", "Bugs Bunny",
    "Scooby-Doo", "Scooby Doo",
    # 한국 IP
    "Pororo", "뽀로로", "Pinkfong", "핑크퐁", "Baby Shark", "아기상어",
    "Larva Animation", "Tayo Bus", "타요버스", "Robocar Poli", "로보카폴리",
    "Cocomong", "코코몽",
    # 책 캐릭터 굿즈 (책 자체는 영문 정책으로 등록, 굿즈는 IP 위반)
    "Hungry Caterpillar", "Eric Carle", "에릭 칼", "배고픈 애벌레",
    "Dr. Seuss", "Cat in the Hat", "Lorax", "Grinch",
    "Goodnight Moon", "Pete the Cat", "Llama Llama",
    "Pat the Bunny", "Mo Willems", "Elephant and Piggie",
    "Boxcar Children", "Magic Tree House", "Diary of a Wimpy Kid",
    "Captain Underpants", "Goosebumps",
    # 인기 장난감 IP
    "Squishmallow", "Funko Pop",
    "Hot Wheels", "핫휠", "Barbie Mattel", "Polly Pocket",
    "My Little Pony", "Transformers", "트랜스포머",
    "Power Rangers", "파워레인저", "G.I. Joe", "GI Joe",
    "L.O.L. Surprise", "LOL Surprise", "Hatchimals", "Ninjago",
    "LEGO", "레고",
    # 게임 IP
    "Minecraft", "마인크래프트", "Fortnite", "포트나이트",
    "Five Nights at Freddy", "Roblox", "Call of Duty",
    # ── 2026-05-08 쿠팡 KSECRET copyright 신고 ──
    # 한국 화장품 브랜드 (라이선스/상표권)
    "K-SECRET", "KSECRET", "케이시크릿",
    # ── 2026-05-08 한국 IP 추가 ──
    # Spigen — 한국 모바일 액세서리 brand (서울 본사)
    "Spigen",
    # ── Tier 8: 2026-05-27 쿠팡 유통경로 확인 요청(브랜드 게이팅) ──
    # "유통경로 확인 요청(Please provide proof of purchase)" 메일 대상. 구매대행은 정품 증빙 불가 → 등록 차단.
    # 영문은 단어경계 매칭이라 안전(\bBOSE\b≠boss/Boston, \bBRAUN\b≠brown).
    # 위험한 한글 substring(보스→보스턴 / 브라운→brown색 / 피노→피노키오)은 제외하고 영문 매칭에 의존.
    "New Balance", "뉴발란스",
    "Under Armour", "언더아머",
    "Nike", "나이키",
    "Logitech", "로지텍",
    "Amos", "아모스",
    "IOPE", "아이오페",
    "JBL", "제이비엘",
    "Bose",
    "Le Creuset", "르크루제",
    "Braun",
    "Starbucks", "스타벅스",
    "The Ordinary", "디오디너리",
    "Estee Lauder", "Estée Lauder", "에스티로더",
    "Pino",
    "Cetaphil", "세타필",
    "Lancome", "Lancôme", "랑콤",
    "Asics", "아식스",
)


# ═══════════════════════════════════════════════════════════
# 2. 효능 표현 (Efficacy Claims) — Sanitize (건강식품 카테고리만)
# ═══════════════════════════════════════════════════════════
# 식약처 「건강기능식품 표시·광고 심의기준」 위반 우려 표현.
# 자율심의 미통과 시 의약품적 효능 표현은 금지.
EFFICACY_CLAIM_PATTERNS = (
    r"면역(?:력)?\s*(강화|증진|향상|개선|지원)",
    r"피로\s*(회복|개선|해소)",
    r"항산화",
    r"노화\s*(방지|억제|예방)",
    r"다이어트(\s*효과)?",
    r"체중\s*(감량|조절|관리)",
    r"혈압\s*(개선|조절|강하)",
    r"혈당\s*(개선|조절|관리)",
    r"콜레스테롤\s*(감소|개선|조절)",
    r"기억력\s*(개선|향상|증진)",
    r"집중력\s*(개선|향상|증진)",
    r"관절\s*(건강|개선)",
    r"눈\s*건강",
    r"간\s*건강",
    r"장\s*건강",
    r"전립선\s*건강",
    r"갱년기\s*(개선|증상)",
    r"숙면|수면\s*(개선|유도)",
    r"불면증?\s*(개선|해소)",
    r"질병\s*(예방|치료)",
    r"질환\s*(예방|치료)",
    r"치료\s*효과",
    # ── 2026-05-04 신규 추가 (적발 사례 기반) ──
    r"설사\s*(완화|개선|예방)",
    r"알레르기\s*(완화|개선|예방|항)",
    r"감기\s*(예방|개선)",
    r"항\s*염증",
    r"면역\s*기능\s*(지원|강화|증진)",
    r"신경\s*(안정|진정)",
    r"근육\s*(이완|회복)",
    r"건강\s*보조",  # "장 건강 보조"
    r"기능\s*지원",  # "면역 기능 지원"

    # ── 2026-05-20 시행 식품 허위과대광고 정책 강화 (네이버 공지 2026-04-20) ──
    # 질병명 + 완치/치료/회복 결합 표현 — 1회 경고, 2회 제재, 3회 이용정지
    r"(?:당뇨|고혈압|비만|불면증?|우울증?|변비|설사|위염|위궤양|역류성?\s*식도염|"
    r"과민성?\s*대장|관절염|류마티스|골다공증|디스크|천식|비염|알레르기|아토피|"
    r"건선|습진|심장병|동맥경화|뇌졸중|치매|알츠하이머|간염|지방간|담석|"
    r"신장병|방광염|전립선염|요로결석|빈혈|백혈병|암|종양|갱년기|생리통|"
    r"자궁근종|난임|탈모|백반증|감기|독감|인후염|편도선염|두통|편두통|"
    r"어지럼증|기관지염|폐렴|결핵|공황장애)\s*(?:완치|치료|회복|개선|낫|치유|예방)",

    # 질병/증상 + "에 좋은/효능"
    r"(?:당뇨|고혈압|관절|간|위|장|신장|혈관|심장|뇌|폐|피부|머리|눈)"
    r"\s*(?:에\s*좋은|효능|효과)",

    # 한약 처방명 (단독 사용도 의약품 오인)
    r"(?:십전대보탕|보중익기탕|사물탕|사군자탕|육미지황탕|팔미지황탕|"
    r"우황청심원|천왕보심단|쌍화탕|갈근탕|소시호탕|인삼양영탕|생맥산|"
    r"공진단|경옥고|총명탕|보약)",

    # 의약품 오인 — 약효 단정 표현
    r"약효",
    r"치유\s*효과",
    r"의학적\s*효능",
    r"임상\s*증명",

    # ── 2026-06-18 해외직구 식품 부당광고 강화 (식품표시광고법 제8조, 식약처 가이드) ──
    # 기능성 오인 (해외직구 식품은 인정 기능성도 사용 불가)
    r"흡수율?\s*(증가|향상|개선|촉진|업|UP)",
    r"디톡스",
    r"해독\s*(작용|효과|기능)",
    r"혈액\s*순환\s*(개선|촉진|원활)?",
    r"혈행\s*(개선|촉진)",
    r"혈관\s*건강",
    r"체지방\s*(감소|분해|연소|관리)",
    r"신진\s*대사\s*(촉진|개선|활성)?",
    r"대사\s*(촉진|활성화)",
    r"피부\s*(미백|주름\s*개선|탄력|보습|재생|진정|개선)",
    r"미백\s*효과",
    r"주름\s*(개선|완화)",
    r"뼈\s*건강",
    r"두뇌\s*(건강|발달|회전)",
    r"심장\s*건강",
    r"폐\s*건강",
    r"스트레스\s*(완화|해소|감소|개선)",
    r"활력\s*(증진|충전|회복|넘치)",
    r"에너지\s*(충전|보충|증진)",
    r"항노화|안티\s*에이징",
    r"세포\s*(재생|보호|활성|복구)",
    r"호르몬\s*(균형|조절)",
    r"부종\s*(완화|개선|제거)",
    # 의약품 오인 — 약효 단정
    r"특효|즉효|속효",
    r"완치|근치",
    r"부작용\s*(없|제로|無|무)",
    r"(소염|진통|해열|살균|항균)\s*(효과|작용)",
    r"항\s*(바이러스|박테리아)\s*(효과|작용)?",
    # 거짓·과장
    r"100\s*%?\s*(효과|효능)",
    r"기적의?|마법의?\s*(효과|효능)?",
    r"세계\s*최고|국내\s*유일|업계\s*최고|효과\s*최고",
    r"검증된\s*효과",
    # 소비자 기만 — 전문가 추천·체험·임상
    r"(의사|약사|한의사|전문가|병원|의료진)\s*(추천|처방|인정|개발)",
    r"임상\s*(실험|시험)\s*(완료|통과|입증)",
)
_EFFICACY_RE = re.compile("|".join(EFFICACY_CLAIM_PATTERNS), re.IGNORECASE)


# ═══════════════════════════════════════════════════════════
# 3. 건강식품 카테고리 식별 (효능 필터 적용 대상)
# ═══════════════════════════════════════════════════════════
HEALTH_FOOD_CATEGORY_KEYWORDS = (
    "건강기능식품", "건강식품", "영양제", "보충제", "프로틴",
    "비타민", "오메가", "유산균", "프로바이오틱", "비오틴",
    "콜라겐", "마그네슘", "철분", "아연", "효소", "코큐텐",
    "단백질파우더", "다이어트식품", "건강분말", "건강즙",
    "한방재료", "환자식", "영양보충식", "숙취해소",
)


# ═══════════════════════════════════════════════════════════
# 4. 취급 불가 카테고리 (Prohibited Categories) — Hard Block
# ═══════════════════════════════════════════════════════════
PROHIBITED_CATEGORIES = (
    "성인용품", "성인",
    "주류", "와인", "맥주", "위스키",
    "담배", "전자담배", "니코틴",
    "도검", "총기", "에어건", "모조총",
    "마약", "대마",
    "의약품",
)


# ═══════════════════════════════════════════════════════════
# 도서 — 오디오북 / ebook 차단 (구매대행 부적합)
# ═══════════════════════════════════════════════════════════
# 종이책은 통과, 오디오북/전자책은 차단.
# 구매대행 = 실물 배송이 본질이라 디지털 콘텐츠는 처리 불가.
PROHIBITED_PRODUCT_TYPES = (
    "ABIS_EBOOKS",                  # Kindle eBooks
    "ABIS_AUDIO_BOOK",              # 일반 오디오북
    "AUDIBLE_AUDIO_EDITION",        # Audible 오디오북
    "DOWNLOADABLE_AUDIO_BOOK",      # 다운로드 오디오북
    "DOWNLOADABLE_VIDEO",           # 디지털 비디오
    "DOWNLOADABLE_MUSIC_TRACK",     # 디지털 음원
    "DIGITAL_VIDEO_GAMES",          # 디지털 게임
    "DIGITAL_SOFTWARE",             # 디지털 소프트웨어
    "DIGITAL_DEVICE_3",             # 디지털 기기 (Kindle 등)
    "PRESSED_AUDIO_BOOK",           # CD 오디오북 (애매하니 차단)
)

# title 키워드 fallback (productType 못 받았을 때)
DIGITAL_BOOK_TITLE_KEYWORDS = (
    "Audible Audiobook", "Audible Original", "Audible Edition",
    "Kindle Edition", "Kindle eBook", "Kindle Single",
    "eBook Edition", "Digital Edition",
    "오디오북", "전자책", "이북",
)


# ═══════════════════════════════════════════════════════════
# 5. 위반 이력 로그 (clean_violation_log 테이블)
# ═══════════════════════════════════════════════════════════
def log_violation(
    stage: str,
    violation_type: str,
    action_taken: str,
    matched_keyword: Optional[str] = None,
    product_id: Optional[int] = None,
    asin: Optional[str] = None,
    channel: Optional[str] = None,
    original_text: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    """clean_violation_log 테이블에 위반 이력 기록.

    스키마 누락 시 silently 실패 (마이그레이션 전 호환성).
    """
    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO clean_violation_log
                   (stage, violation_type, action_taken, matched_keyword,
                    product_id, asin, channel, original_text, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (stage, violation_type, action_taken, matched_keyword,
                 product_id, asin, channel,
                 (original_text or "")[:500] if original_text else None,
                 notes),
            )
    except Exception as e:
        logger.warning(f"[clean_policy] 위반 로그 기록 실패: {e}")


# ═══════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════

def check_prohibited_ingredients(
    title_en: str = "",
    title_ko: str = "",
    description: str = "",
) -> tuple[bool, Optional[str]]:
    """상품명/설명에 금지 성분이 있는지 검사.

    Returns:
        (True, matched_keyword) — 차단 대상
        (False, None) — 통과
    """
    haystack = f"{title_ko or ''} {title_en or ''} {description or ''}"
    haystack_upper = haystack.upper()
    for kw in PROHIBITED_INGREDIENTS:
        if not kw:
            continue
        if re.search(r"[A-Za-z]", kw):
            # 영문은 단어 경계 검사 (false positive 방지)
            if re.search(rf"\b{re.escape(kw.upper())}\b", haystack_upper):
                return True, kw
        else:
            if kw in haystack:
                return True, kw
    return False, None


def check_prohibited_category(category_path: str) -> tuple[bool, Optional[str]]:
    """카테고리 경로에 취급불가 카테고리가 있는지 검사."""
    if not category_path:
        return False, None
    for kw in PROHIBITED_CATEGORIES:
        if kw in category_path:
            return True, kw
    return False, None


# ═══════════════════════════════════════════════════════════
# 취급제외 아마존 카테고리 — 거울/벽걸이 (액자·가구와 동일 취지, 2026-07-05)
# 파손·대형 데코라 국제배송 부적합. amazon_category_json 노드명 정확 매칭
# (자동차 거울·소형 손거울·벽 스티커는 미포함 = 사장님 확정 범위).
# ═══════════════════════════════════════════════════════════
EXCLUDE_CATEGORIES_AMAZON = (
    "Wall-Mounted Mirrors", "Makeup Mirrors", "Floor & Full Length Mirrors",
    "Mirrors", "Wall-Mounted Vanity Mirrors", "Mirror Sets",
    "Wall-Mounted", "Wall Décor", "Shower & Wall Mounts", "Wall-Mounted Wine Racks",
)


def _amazon_category_names(js):
    """amazon_category_json(브레드크럼 리스트) → 노드 name 집합."""
    if not js:
        return set()
    try:
        import json as _json
        data = _json.loads(js)
    except Exception:
        return set()
    out = set()
    if isinstance(data, list):
        for c in data:
            if isinstance(c, dict) and c.get("name"):
                out.add(c["name"])
    return out


# ═══════════════════════════════════════════════════════════
# 이미지 정책 카테고리 — 화장품/건강기능식품 = 저작권 신고 고위험 → 자체이미지(누끼/AI) 필수 (2026-07-06)
# 그 외 = 아마존 원본 사용. amazon_category_json 노드명 기준.
# ═══════════════════════════════════════════════════════════
COSMETIC_CATEGORIES = (
    "Serums", "Facial Serums", "Face Moisturizers", "Moisturizers", "Tinted Moisturizers",
    "Creams", "Creams & Moisturizers", "Night Creams", "Day Creams", "Eye Creams",
    "BB Creams", "CC Creams", "Hand Creams & Lotions", "Foot Creams & Lotions",
    "Balms & Moisturizers", "Makeup Cleansing Creams", "Lotions", "Facial Sunscreens",
    "Body Sunscreens", "Sunscreens", "Sleeping Masks", "Masks", "Hair Masks",
    "Color Refreshing Masks", "Facial Kits", "Cleansers", "Toners & Astringents",
    "Essences", "Ampoules", "Exfoliators", "Facial Peels", "Anti-Aging Products",
    "Lip Balms & Moisturizers", "Skin Care Sets", "Face Oils", "Facial Treatments",
)
SUPPLEMENT_CATEGORIES = (
    "Collagen", "Omega-3", "Omega 3-6-9", "Fish Oil Supplements", "Multivitamins",
    "Blended Vitamin & Mineral Supplements", "Multiminerals", "Minerals", "Trace Minerals",
    "Probiotics", "Probiotics & Digestive Supplements", "Digestive Supplements",
    "Sleep Supplements", "Herbal Supplements", "Supplements", "Supplements & Vitamins",
    "Vitamins", "Vitamins, Minerals & Supplements", "Vitamins Minerals & Supplements",
    "Vitamin C", "Vitamin D", "Vitamin E", "Vitamin B", "Vitamin A",
    "Vitamin D3 & K2 Combinations", "Prenatal Vitamins", "Children's Vitamins",
    "Spirulina", "Chlorella", "Antioxidant Supplements", "Greens", "Superfood Supplements",
)


def classify_image_policy(amazon_category_json) -> str:
    """화장품/건강기능식품이면 'self_made'(누끼/AI 자체이미지 필수), 아니면 'amazon'(원본 사용)."""
    names = _amazon_category_names(amazon_category_json)
    if not names:
        return "amazon"
    hi = set(COSMETIC_CATEGORIES) | set(SUPPLEMENT_CATEGORIES)
    return "self_made" if (names & hi) else "amazon"


# ── 채널 배정용 카테고리 3분류 (2026-08-03 사장 지시) ───────────────
#   화장품        → 네이버 신계정 (화장품 전용 몰)
#   식품/건기식    → 네이버 구계정
#   그 외         → 쿠팡 (구/신은 임포트 시 지정)
#   ※ 기본은 배타 배정. 나중에 "쿠팡에도 올려" 지시가 있으면 플래그를 추가로 켠다.
FOOD_CATEGORIES = (
    "Grocery & Gourmet Food", "Grocery", "Gourmet Food", "Food",
    "Snack Foods", "Snacks", "Beverages", "Coffee", "Tea", "Coffee, Tea & Cocoa",
    "Breakfast Foods", "Cereals", "Pantry Staples", "Canned & Jarred Foods",
    "Condiments & Salad Dressings", "Sauces", "Herbs, Spices & Seasonings",
    "Cooking & Baking", "Baking Supplies", "Oils, Vinegars & Salad Dressings",
    "Dried Fruits & Vegetables", "Nuts & Seeds", "Candy & Chocolate",
    "Jams, Jellies & Sweet Spreads", "Soups, Stocks & Broths", "Pasta & Noodles",
    "Rice & Grains", "Meal Replacement", "Sports Nutrition", "Protein Powders",
    "Nutrition Bars", "Baby Food", "Honey", "Syrups", "Seasonings",
)


def classify_target_channel(amazon_category_json) -> str:
    """채널 배정 분류 — 'cosmetic' | 'food' | 'general'.
    카테고리 정보가 없으면 'general'(쿠팡)로 보수적 처리."""
    names = _amazon_category_names(amazon_category_json)
    if not names:
        return "general"
    # 화장품 우선 — 화장품과 식품 카테고리가 함께 잡히면 화장품으로 본다
    if names & set(COSMETIC_CATEGORIES):
        return "cosmetic"
    if names & (set(SUPPLEMENT_CATEGORIES) | set(FOOD_CATEGORIES)):
        return "food"
    return "general"


def check_excluded_amazon_category(product_id=None, parent_asin=None,
                                   amazon_category_json=None):
    """아마존 카테고리에 취급제외(거울/벽걸이류) 노드가 있으면 차단.

    product_id 또는 parent_asin 주면 DB에서 amazon_category_json 조회.
    Returns (blocked, matched_name).
    """
    js = amazon_category_json
    if js is None:
        try:
            with get_db() as conn:
                if product_id is not None:
                    row = conn.execute(
                        "SELECT amazon_category_json FROM products WHERE id=?",
                        (product_id,)).fetchone()
                    js = row["amazon_category_json"] if row else None
                elif parent_asin:
                    row = conn.execute(
                        "SELECT amazon_category_json FROM products "
                        "WHERE parent_asin=? AND amazon_category_json IS NOT NULL LIMIT 1",
                        (parent_asin,)).fetchone()
                    js = row["amazon_category_json"] if row else None
        except Exception:
            return False, None
    names = _amazon_category_names(js)
    for kw in EXCLUDE_CATEGORIES_AMAZON:
        if kw in names:
            return True, kw
    return False, None


# ═══════════════════════════════════════════════════════════
# KC 비면제 품목 — "KC마크 없이 구매대행 불가" (2026-05-23)
# ═══════════════════════════════════════════════════════════
# 어린이제품(전수) + 아래 전기용품/생활용품 안전인증·안전확인 대상은
# 구매대행 KC 면제(kcExemptionType=OVERSEAS)가 적용되지 않음 → 리스팅 차단.
#   - 어린이제품: coupang_meta noticeCategories='어린이제품' 로 정확 검출
#   - 전기/생활: 키워드 매칭. ★false positive 방지 위해 구체/다단어만 사용
#     (의도적 제외 — 일반어라 과차단 우려: 일반조명기구/유체펌프/기포발생기/케이블·코드류)
#   - 비비탄총/배터리(충전지)는 PROHIBITED_CATEGORIES(총기) / Tier6 배터리로도 이미 커버
KC_NON_EXEMPT_KEYWORDS = (
    # ── 전기용품 ──
    "battery charger", "phone charger", "wireless charger", "usb charger",
    "fast charger", "wall charger", "car charger", "charging station", "charging dock",
    "충전기",
    "water purifier", "water ionizer", "정수기", "이온수기",
    "electric heating pad", "heating mat", "electric blanket", "heated blanket",
    "전기매트", "온수매트", "전기요", "전기장판", "전기찜질",
    "air compressor", "컴프레서", "컴프레셔",
    "power supply unit", "전원공급장치",
    "electric treadmill", "전동 러닝머신", "전기헬스",
    # ── 생활용품 ──
    "gas lighter", "가스라이터",
    "retread tire", "retreaded tire", "재생타이어", "재생 타이어",
    "bb gun", "airsoft", "비비탄",
)


# 어린이 완구/캐릭터 브랜드 — 어린이제품(KC 안전인증 대상)으로 확정 차단.
# ★카테고리 메타(noticeCategories)는 coupang_category_code 가 있을 때만 동작하는데
#  listed 의 91%가 자동매칭(코드 미상)이라 검출이 스킵됨 → 브랜드 폴백으로 보강 (2026-06-02).
#  ★브랜드 기준이라 펫·보관용품 오탐 없음(완구브랜드는 그쪽에 안 붙음).
#  성인 수집피규어(Banpresto/Tamashii/Super7/NECA/Funko/Hot Toys 등)는 의도적 제외(15세+).
CHILDREN_TOY_BRANDS = frozenset(b.lower() for b in (
    "Barbie", "Mattel", "Hot Wheels", "LEGO", "Fisher-Price", "Hasbro", "Play-Doh",
    "Nerf", "Melissa & Doug", "Calico Critters", "Playmobil", "Paw Patrol", "Bluey",
    "Little Tikes", "VTech", "Crayola", "Step2", "Klutz", "Ravensburger", "Aurora",
    "Spin Master", "MGA", "Schleich", "CoComelon", "Pokemon", "Breyer",
    "KIDS PREFERRED", "Squishmallows", "Polly Pocket", "My Little Pony",
    "Transformers", "Power Rangers", "Thomas & Friends", "Peppa Pig", "Pinkfong",
))


# ── 어린이제품 카테고리 경로 마커 (2026-06-30) ───────────────────────────────
#  noticeCategories 의 '어린이제품' 글자는 캠핑의자(81867)·반려장난감·선크림 등 수많은
#  다중고시 카테고리에 "선택 가능한 상품고시 템플릿"으로 들어가 있어, 그 존재만으로 차단하면
#  성인 제품이 대량 오탐된다(실측: 캠핑의자 77건 등). → '어린이제품' 고시 AND 카테고리 경로가
#  실제 영유아동 카테고리일 때만 KC 차단(고시+경로 결합, 사장님 승인 2026-06-30).
_KIDS_PATH_MARKERS = ("출산/유아동", "영유아동", "유아동", "신생아", "영아완구", "유아완구")


def _category_path_is_kids(coupang_category_code) -> bool:
    """쿠팡 카테고리 경로가 실제 영유아동(어린이제품) 카테고리인지. coupang_categories.path 기준."""
    if not coupang_category_code:
        return False
    try:
        with get_db() as conn:
            r = conn.execute(
                "SELECT path FROM coupang_categories WHERE code=?",
                (str(coupang_category_code),),
            ).fetchone()
        path = (r["path"] if r else "") or ""
    except Exception:
        return False
    if not path:
        return False
    if any(m in path for m in _KIDS_PATH_MARKERS):
        return True
    # 완구/취미 하위의 아동 완구만(성인 수집/취미 프라모델·보드게임 등 제외 위해 아동 키워드 동반 시)
    if path.startswith("완구/취미") and any(k in path for k in ("어린이", "아동", "키즈", "신생아", "영아", "유아", "토들러", "주니어")):
        return True
    return False


# ── 어린이제품 연령표기·완구 키워드 (2026-08-05) ────────────────────────────
#  기존 게이트는 ①완구 브랜드 화이트리스트(35개) ②카테고리 메타(코드 부재 66.8%)
#  ③KC 비면제 키워드(전기/생활용품 전용)뿐이라 통과율 96% 였다.
#  상품명의 '8-12세'·'3세 이상' 같은 연령표기가 어린이제품의 가장 강한 신호인데
#  전혀 보지 않았다 → Apitor 로봇 STEM 8-12세가 KC 제재 후에도 재등록됨.
_KC_AGE_RE = re.compile(
    r"(\d{1,2}\s*[-~ㅡ–]\s*\d{1,2}\s*세)"          # 8-12세
    r"|(만\s*\d{1,2}\s*세)"                          # 만 5세
    r"|(\d{1,2}\s*세\s*(이상|용|부터|이하))"          # 3세 이상 / 5세용
    r"|(\bages?\s*\d{1,2}\b)"                       # ages 3
    r"|(\d{1,2}\s*\+\s*(years|yrs|세))",            # 3+ years
    re.I)
# ★STEM 은 단어경계+대문자 전용(2026-08-05 수정) — '밸브 스템 캡'(자동차 부품)이
#   과학교육 STEM 으로 오차단됐다. 한글 '스템'은 매치하지 않는다.
_KC_TOY_RE = re.compile(
    r"(\bSTEM\b)|(코딩\s*로봇)|(코딩로봇)|(몬테소리)|(교구)|(퍼즐놀이)"
    r"|(블록\s*세트)|(자석\s*블록)|(원목\s*블록)|(놀이\s*세트)"
    r"|(역할놀이)|(소꿉)|(미술놀이)|(과학\s*실험\s*키트)|(실험\s*키트)")
# 가드 — 반려동물 완구와 성인 연령표기는 어린이제품이 아니다.
_KC_PET_RE = re.compile(r"강아지|애견|반려|고양이|소형견|중형견|대형견|캣타워|노즈워크")
_KC_ADULT_RE = re.compile(r"1[89]\s*세|성인용|어른용|20\s*세\s*이상")

# ── 어린이 직접표기 (2026-08-07) ────────────────────────────────────────────
#  쿠팡 2차 반려(옵션 95587243061 'Ooly 어린이 DIY 슈링클 아트 키트')의 직접 원인.
#  위 규칙들엔 '어린이·키즈·유아·아동'이라는 단어가 아예 없어서, 상품명에 대놓고
#  어린이라고 적힌 상품이 게이트를 그냥 통과했다. 실측 2,461건이 라이브에 남아 있었다.
#  ★'소년'은 '청소년'(만 13세 초과, 어린이제품 아님)에 걸리므로 부정후방탐색으로 뺀다.
_KC_KID_WORD_RE = re.compile(
    r"어린이|영유아|유아동|유아|아동|키즈|소아|신생아|유치원|어린이집|미취학|초등학생"
    r"|젖병|기저귀|유모차|보행기|카시트|(?<!청)소년|소녀"
    r"|\bkids?\b|\bchildren'?s?\b|\btoddlers?\b|\binfants?\b"
    r"|\bnursery\b|\bpreschool\b|\bstroller\b|\bpacifier\b",
    re.I)


def _kc_kids_by_title(title_ko: str, title_en: str = "") -> Optional[str]:
    """상품명 기반 어린이제품 판정. 반환: 사유 or None."""
    hay = f"{title_ko or ''} {title_en or ''}"
    if not hay.strip():
        return None
    # ★어린이 직접표기는 반려동물·성인 가드보다 우선 (2026-08-07).
    #   '어린이 성인 공용 물총'도 어린이가 쓰면 어린이제품이고,
    #   물놀이기구는 KC 안전인증 4종 지정품목이다.
    m = _KC_KID_WORD_RE.search(hay)
    if m:
        return f"어린이제품(어린이 표기 '{m.group(0).strip()}')"
    if _KC_PET_RE.search(hay) or _KC_ADULT_RE.search(hay):
        return None
    m = _KC_AGE_RE.search(hay)
    if m:
        return f"어린이제품(연령표기 '{m.group(0).strip()}')"
    m = _KC_TOY_RE.search(hay)
    if m:
        return f"어린이제품(완구·교구 '{m.group(0).strip()}')"
    return None


# ── 브랜드 필드 기준 차단 (2026-08-05) ─────────────────────────────────────
#  기존 _is_brand_blocked 는 상품명만 본다. 그래서
#   ①한글표기 상품('루프 Engage 2 이어플러그')을 못 막고
#   ②일반명사 브랜드를 등재하면 대량 오차단('hook and loop' 벨크로 609건)
#  products.brand 는 라이브 99.7% 채워져 있으니 정확 대조가 안전하다.
#  ★목록은 settings.brand_field_blocklist 로 분리 — 제목매칭용과 섞지 말 것.
_BRAND_FIELD_BLOCKLIST_CACHE = None


def _load_brand_field_blocklist() -> frozenset:
    global _BRAND_FIELD_BLOCKLIST_CACHE
    if _BRAND_FIELD_BLOCKLIST_CACHE is not None:
        return _BRAND_FIELD_BLOCKLIST_CACHE
    vals = []
    try:
        from backend.purchase.database import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key='brand_field_blocklist'").fetchone()
        if row and row["value"]:
            vals = json.loads(row["value"])
    except Exception as e:
        logger.warning(f"[kc] brand_field_blocklist 로드 실패: {e}")
    _BRAND_FIELD_BLOCKLIST_CACHE = frozenset(
        str(v).strip().lower() for v in vals if str(v).strip())
    return _BRAND_FIELD_BLOCKLIST_CACHE


def check_brand_field_blocked(brand: str) -> tuple[bool, Optional[str]]:
    """products.brand 정확 대조 차단. 상품명은 보지 않는다.

    일반명사형 브랜드(Loop·Odyssey·UNO·Dawn 등)를 오차단 없이 막기 위한 경로.
    """
    b = (brand or "").strip().lower()
    if not b or b in ("generic", "unbranded", "none", "해외 브랜드"):
        return False, None
    if b in _load_brand_field_blocklist():
        return True, f"브랜드필드 차단({brand.strip()})"
    return False, None


# ── 삭제 ASIN 재등록 차단 (2026-08-05) ─────────────────────────────────────
#  Apitor 가 구계정 KC 제재로 삭제된 뒤 신계정에 재등록된 사고가 있었다.
#  브랜드 블랙리스트는 표기 변형(루프/Loop)에 뚫리지만 ASIN 은 고유하다.
#  ★계정 무관 — 구계정 삭제분도 신계정 등록 시 차단한다.
def check_blocked_asin(asin: str) -> tuple[bool, Optional[str]]:
    """과거 3축(리셀금지·한국브랜드역수입·KC어린이제품) 사유로 삭제된 ASIN 인가."""
    a = (asin or "").strip().upper()
    if not a:
        return False, None
    try:
        from backend.purchase.database import get_db
        with get_db() as conn:
            r = conn.execute(
                "SELECT axis, reason FROM blocked_products WHERE UPPER(asin)=? LIMIT 1",
                (a,)).fetchone()
        if r:
            return True, f"삭제이력 재등록차단({r['axis']}: {str(r['reason'])[:40]})"
    except Exception as e:
        logger.warning(f"[block] blocked_products 조회 실패 {a}: {e}")
    return False, None


# 어린이제품 안전 특별법: 만 13세 "이하"가 사용하는 물품.
# 만 13세대(156~167개월)도 포함되므로 경계는 만 14세 = 168개월이다.
KC_KIDS_MAX_MONTHS = 168


def _kc_kids_by_sp_age(asin: str = "", product_id=None):
    """products.sp_kc_min_age_months 로 어린이제품 판정 (2026-08-06 신설).

    sp_api_facts 가 SP-API attributes 에서 낮은연령 우선으로 도출해 채운 값이다.
    상품명 추론과 달리 제조사 신고값이라 이쪽이 정확하다.
    """
    if not asin and product_id is None:
        return None
    try:
        from backend.purchase.database import get_db
        with get_db() as conn:
            if product_id is not None:
                row = conn.execute(
                    "SELECT sp_kc_min_age_months m, sp_age_range_desc d "
                    "FROM products WHERE id=?", (product_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT sp_kc_min_age_months m, sp_age_range_desc d "
                    "FROM products WHERE asin=? AND sp_kc_min_age_months IS NOT NULL "
                    "LIMIT 1", (asin.strip().upper(),)).fetchone()
        if not row or row["m"] is None:
            return None
        m = float(row["m"])
        if m < KC_KIDS_MAX_MONTHS:
            return ("어린이제품(SP-API 제조사 최소연령 %.0f개월 = 만 %.1f세%s)"
                    % (m, m / 12.0, (", 표기 " + str(row["d"])) if row["d"] else ""))
    except Exception:
        return None
    return None


def check_kc_blocked(
    title_en: str = "",
    title_ko: str = "",
    coupang_category_code=None,
    brand: str = "",
    asin: str = "",
    product_id=None,
) -> tuple[bool, Optional[str]]:
    """KC마크 없이 구매대행 불가 품목 검사.

    Returns:
        (True, reason) — 차단 (리스팅 불가, kc_required)
        (False, None)  — 통과
    """
    # 0-a. ★SP-API 제조사 최소연령 (2026-08-06) — 권위값이라 가장 먼저 본다.
    #      값이 없을 때만 아래 카테고리·상품명 추론으로 내려간다.
    _sp = _kc_kids_by_sp_age(asin=asin, product_id=product_id)
    if _sp:
        return True, _sp

    # 0. 어린이 완구 브랜드 — 카테고리 코드 없이도 차단 (자동매칭 우회 보강)
    if brand and brand.strip().lower() in CHILDREN_TOY_BRANDS:
        return True, f"어린이제품(완구 브랜드 {brand.strip()})"

    # 1. 어린이제품 — 고시('어린이제품') AND 카테고리 경로(영유아동) 결합 (2026-06-30)
    #    ★단순 noticeCategories 글자 매칭은 다중고시 카테고리(캠핑의자·반려장난감·선크림 등)에서
    #      대량 오탐 → 카테고리 경로가 실제 영유아동일 때만 차단(_category_path_is_kids).
    if coupang_category_code:
        try:
            from backend.purchase.services import coupang_meta
            meta = coupang_meta.get_category_meta(str(coupang_category_code))
            has_kid_notice = bool(meta) and any(
                "어린이제품" in (n.get("noticeCategoryName") or "")
                for n in (meta.get("noticeCategories") or [])
            )
            if has_kid_notice and _category_path_is_kids(coupang_category_code):
                return True, "어린이제품(KC 안전인증 대상)"
        except Exception:
            pass  # 메타 조회 실패 시 키워드만으로 진행 (보수적 통과)

    # 1-b. 상품명 연령표기·완구 키워드 (2026-08-05 보강)
    #      카테고리 코드가 없거나 브랜드가 화이트리스트에 없어도 잡는다.
    _kid = _kc_kids_by_title(title_ko, title_en)
    if _kid:
        return True, _kid

    # 2. 전기/생활용품 비면제 키워드 (영문=단어경계, 한글=substring)
    haystack = f"{title_ko or ''} {title_en or ''}"
    hu = haystack.upper()
    for kw in KC_NON_EXEMPT_KEYWORDS:
        if re.search(r"[A-Za-z]", kw):
            if re.search(rf"\b{re.escape(kw.upper())}\b", hu):
                return True, kw
        else:
            if kw in haystack:
                return True, kw
    return False, None


def check_prohibited_book(
    title_en: str = "",
    product_type: str = "",
) -> tuple[bool, Optional[str]]:
    """도서 — 오디오북/ebook/디지털 콘텐츠 차단.

    Args:
        title_en: 영문 상품명 (productType 없을 때 fallback)
        product_type: SP-API 응답의 productType 값

    Returns:
        (True, '오디오북' or 'ebook' or matched keyword) — 차단
        (False, None) — 통과
    """
    # 1) productType 정확 매칭 (우선)
    if product_type:
        pt_upper = product_type.upper()
        if pt_upper in PROHIBITED_PRODUCT_TYPES:
            return True, product_type

    # 2) title 키워드 fallback
    if title_en:
        for kw in DIGITAL_BOOK_TITLE_KEYWORDS:
            if kw.lower() in title_en.lower():
                return True, kw

    return False, None


def is_health_food_category(category_path: str) -> bool:
    """건강식품 카테고리 여부 (효능 필터 적용 판단)."""
    if not category_path:
        return False
    return any(kw in category_path for kw in HEALTH_FOOD_CATEGORY_KEYWORDS)


# ── DTC 유전자검사 키트 (생명윤리법 제49조1항 위반 — 영구 차단) ───
# 2026-05-30 국가생명윤리정책원 제도개발팀 모니터링 적발건. 5건 stop_sales 후 영구 차단.
GENETIC_KIT_KEYWORDS = (
    # 영문 brand/term
    "dtc genetic", "genetic test kit", "dna test kit", "dna testing kit",
    "ancestry dna", "ancestrydna", "23andme", "23 and me", "myheritage dna",
    "living dna", "tellmegen", "vitagene", "nebula genomics", "familytreedna",
    "saliva dna", "saliva collection kit",
    "genealogy dna", "ancestry test", "heritage dna",
    # 한글
    "유전자검사", "유전자 검사", "dtc 유전자", "dna 검사키트", "dna 검사 키트",
    "조상 dna", "혈통 dna", "유전체 검사", "유전자 키트", "유전자키트",
)
_GENETIC_KIT_LC = tuple(k.lower() for k in GENETIC_KIT_KEYWORDS)


def check_prohibited_genetic_kit(title_en: Optional[str], title_ko: Optional[str]) -> tuple[bool, Optional[str]]:
    """DTC 유전자검사 키트 검출 — 생명윤리법 제49조1항 위반 영구 차단."""
    haystack = (" ".join([s or "" for s in (title_en, title_ko)])).lower()
    if not haystack.strip():
        return False, None
    for kw in _GENETIC_KIT_LC:
        if kw in haystack:
            return True, kw
    return False, None


# ── 도수/광학 보정 의료기기 차단 (의료기기법) ────────────
# 노안/근시 도수 조정 = 의료기기 분류. 일반 셀러는 판매 불가 (식약처 의료기기 판매업 신고 필요).
OPTICAL_DIOPTER_KEYWORDS = (
    "다초점", "근시", "노안", "도수", "도수조절", "안경렌즈",
    "diopter", "dioptre", "hyperopia", "myopia", "presbyopia",
    "bifocal", "multifocal", "progressive lens", "reading lens",
    "prescription lens", "prescription mask", "prescription goggle",
    "rx lens", "rx mask", "corrective lens", "vision correction",
)
_OPTICAL_LC = tuple(k.lower() for k in OPTICAL_DIOPTER_KEYWORDS)

# 도수 표기 패턴 — "+1.0", "+2.5", "-3.0" 같은 광학 도수
import re as _re_optical
_OPTICAL_DIOPTER_RE = _re_optical.compile(r"[+\-]\d+\.\d+(?!\s*[a-z%])", _re_optical.IGNORECASE)


def check_optical_medical_device(
    title_en: Optional[str], title_ko: Optional[str],
    size_label: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """도수/광학 보정 의료기기 검출 — 의료기기법 분류 가능 상품 영구 차단.

    검사 대상:
      1) 키워드 (다초점, 도수, hyperopia, bifocal 등)
      2) size_label 안 도수 패턴 (+1.0, +2.5 등)
    """
    haystack = (" ".join([s or "" for s in (title_en, title_ko, size_label)])).lower()
    if not haystack.strip():
        return False, None
    for kw in _OPTICAL_LC:
        if kw in haystack:
            return True, f"의료기기 분류 가능 키워드: {kw}"
    if size_label and _OPTICAL_DIOPTER_RE.search(size_label):
        return True, f"size_label 안 도수 패턴: {size_label[:40]}"
    return False, None


# ── 의류·신발 임시 차단 (사장님 별도 지시 전까지) ────────────
# 해제: 환경변수 PA_DISABLE_APPAREL_SHOES_BLOCK=1
APPAREL_SHOES_KEYWORDS = (
    # 의류
    "티셔츠", "반팔", "긴팔", "후디", "후드티", "맨투맨", "스웨트셔츠",
    "스웨터", "니트", "카디건", "자켓", "재킷", "점퍼", "잠바",
    "원피스", "드레스", "청바지", "데님", "스웨트팬츠", "조거팬츠",
    "트레이닝팬츠",
    # 하의·셔츠류 보강(2026-06-29) — 반바지 등 누락분
    "반바지", "숏팬츠", "바지", "팬츠", "슬랙스", "레깅스", "치마", "스커트",
    "셔츠", "남방", "블라우스", "폴로셔츠", "파카",
    "양말", "스타킹",
    # 우의류(2026-06-29) — 레인수트/우의 등
    "우의", "우비", "비옷", "레인코트", "레인 수트", "레인수트", "방수복", "수트", "윈드브레이커",
    # 상의·이너·수영복 보강(2026-07-08) — 탱크탑/민소매/속옷/수영복 등 누락
    "탱크탑", "탱크 탑", "민소매", "캐미솔", "카미솔", "브라탑", "스포츠브라", "스포츠 브라",
    "크롭탑", "크롭 탑", "홀터넥", "속옷", "언더웨어", "브래지어", "란제리",
    "잠옷", "파자마", "내복", "수영복", "비키니", "래시가드", "보드숏",
    # 임산부·수유 의류 보강(2026-07-18) — 임신용품 CSV 임포트에서 다수 누락
    "임부복", "출산복", "임신복", "수유복", "산후복", "임산부복", "수유브라",
    "브라렛", "브라 렛", "브라캡", "브라컵", "젖먹이복", "가운", "로브",
    "롬퍼", "점프수트", "점프슈트", "팬티", "슬립원피스",
    "요가 팬츠", "요가팬츠", "요가복", "요가 웨어", "요가웨어", "레인부츠",
    # 신발
    "운동화", "스니커즈", "부츠", "샌들",
)
# 영문 의류·신발 키워드 (2026-07-18) — title_ko 부실 케이스 대응.
# 대소문자 무관, 단어 경계 기반이 아닌 substring 매칭이라 흔한 접두어는 정확히 (예: pants→sweatpants OK).
APPAREL_SHOES_KEYWORDS_EN = (
    # 의류 (일반)
    "t-shirt", "tshirt", "t shirt", " tee ", "tank top", "tanktop",
    "shirt", "blouse", "polo", "dress", "gown ", "gown,", "gown.",
    "pants", "leggings", "jeans", "denim", "trousers", "shorts",
    "skirt", "skort", "jumper", "jumpsuit", "romper", "onesie", "onesies",
    "hoodie", "hoody", "sweater", "sweatshirt", "cardigan",
    "jacket", "coat", "parka", "windbreaker", "poncho",
    # 임산부·수유 (nursing/maternity)
    "maternity", "nursing", "postpartum",
    "bra", "bras", "brassiere", "camisole", "cami ", "lingerie",
    "underwear", "underpants", "panties", "panty",
    "pajama", "pajamas", "pjs", "sleepwear", "nightgown", "nightwear",
    "robe", "loungewear",
    # 수영복·이너
    "swimsuit", "swimwear", "bikini", "rash guard", "rashguard",
    # 신발
    "sneakers", "sneaker", "loafer", "loafers", "flat shoes", "high heel",
    "boots", "sandals", "clogs",
)
# false positive 제외 키워드 (피규어·캐릭터·굿즈·카드·바인더 등)
_APPAREL_FP_EXCLUDES = (
    "피규어", "캐릭터", "굿즈", "케이스", "보관함", "정리함",
    "끈", "액세서리", "악세서리", "피겨", "카드", "바인더",
    "트레이딩", "보드게임",
    # 의류 오탐 제외(2026-06-29): 앞치마(에이프런)·점퍼스(무릎밴드)·수납/보관(의류 커버 가방)·수트케이스(여행가방)
    "앞치마", "점퍼스", "수납", "보관", "정리함", "수트케이스", "슈트케이스",
    # 가구·펫 오탐(2026-07-08): 강아지침대(데님커버) 등 원단명 매칭 방지
    "침대", "매트리스", "쿠션", "방석", "강아지", "반려", "펫", "소파", "커버",
)
_APPAREL_FP_EXCLUDES_EN = (
    # 임산부 굿즈·용품 (의류 아님)
    "pillow", "belt", "band", "wedge", "brace", "support belt",
    "kit", "book", "journal", "diary", "planner", "scrapbook",
    "cream", "oil", "balm", "lotion", "soak", "sanitizer",
    "ball", "wheel", "mat ", "chair", "seat", "stool",
    "bag ", "backpack", "organizer", "storage",
    # 액세서리 (모자·양말 오탐 방지 X, 별도 처리)
    "figure", "figurine", "card", "sticker",
)
# 카테고리 경로 기반 차단 키워드 (2026-07-18 신설).
# title_ko/en 없어도 카테고리에 이 문자열 있으면 무조건 의류 판정.
_APPAREL_CATEGORY_KEYWORDS = (
    "패션의류잡화", "패션의류/잡화", "패션의류", "속옷/잠옷", "언더웨어",
    "여성의류", "남성의류", "아동의류", "유아의류", "임부복", "수영복",
    "잠옷/속옷", "이너웨어", "패션잡화",
)


def check_blocked_apparel_shoes(
    title_ko: Optional[str] = None,
    title_en: Optional[str] = None,
    category_path: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """의류·신발 임시 차단. 환경변수 PA_DISABLE_APPAREL_SHOES_BLOCK=1 로 해제.

    검사 순서 (사장님 지시 2026-07-18):
      1. 카테고리 경로에 의류 키워드 포함 → 무조건 차단 (title 없어도)
      2. title_ko 한글 키워드 (기존 목록 + 임산부·수유 보강)
      3. title_en 영문 키워드 (title_ko 부실 케이스 안전망)
    """
    import os as _os
    if _os.environ.get("PA_DISABLE_APPAREL_SHOES_BLOCK", "").strip() in ("1", "true", "yes"):
        return False, None

    # 1) 카테고리 경로 기반 (title 없어도 차단)
    if category_path:
        for cat_kw in _APPAREL_CATEGORY_KEYWORDS:
            if cat_kw in category_path:
                return True, f"카테고리={cat_kw}"

    # 2) 한글 title 키워드
    if title_ko:
        # false positive 우선 (한글)
        _skip_ko = any(ex in title_ko for ex in _APPAREL_FP_EXCLUDES)
        if not _skip_ko:
            for kw in APPAREL_SHOES_KEYWORDS:
                if kw in title_ko:
                    return True, kw

    # 3) 영문 title 키워드 (title_ko 없거나 매칭 안 될 때 안전망)
    if title_en:
        te_low = title_en.lower()
        _skip_en = any(ex in te_low for ex in _APPAREL_FP_EXCLUDES_EN)
        if not _skip_en:
            for kw in APPAREL_SHOES_KEYWORDS_EN:
                if kw in te_low:
                    return True, f"en:{kw.strip()}"

    return False, None


# ── 의약외품 (약사법) — 무허가 의약외품 광고/판매 차단 (2026-06-13) ──
# 쿠팡 적발: U by Kotex 탐폰 (약사법 제61조의2제1항·제66조). 의약외품은 수입/허가
# 자격 없이 구매대행 등록 불가 — 반복 위반 시 계정정지. 정밀 키워드(오탐 최소).
# 마스크는 정수필터(UKF8001) 오탐 방지 위해 "마스크" 동반 시에만 차단.
QUASI_DRUG_KEYWORDS = (
    # 생리용품
    "탐폰", "tampon", "생리대", "팬티라이너", "팬티 라이너", "생리컵", "menstrual cup",
    # 염모제(영구·새치 염색)
    "염모제", "염색약", "새치 염색", "새치커버", "새치 커버", "hair dye", "hair colour",
    # 제모제
    "제모제", "제모 크림", "depilatory",
    # 살균·손소독
    "손소독제", "손 소독제", "살균소독제", "살균 소독제", "hand sanitizer",
    # 구강·치아(의약외품)
    "구강청결제", "가글제", "치아미백제", "치아 미백제", "teeth whitening",
    # 기피(의약외품)
    "모기기피", "모기 기피", "벌레기피",
    # 콘돔
    "콘돔", "condom",
)
# 섬유·공예 염색 등 오탐 방지 (염모제 아님)
_QUASI_DRUG_FP_EXCLUDES = (
    "타이다이", "tie dye", "tie-dye", "패브릭 염색", "섬유 염색",
    "가죽 염색", "신발 염색", "물감", "립 틴트", "아이 틴트",
)
# 보건용/수술용/의료용 마스크 — "마스크"/"mask" 동반 시에만 (정수필터 UKF 오탐 방지)
_QUASI_DRUG_MASK_KEYWORDS = (
    "보건용", "수술용", "의료용", "비말차단", "kf94", "kf80", "kf-94", "kf-80", "kf99",
)


def check_quasi_drug(title_ko, title_en=None):
    """의약외품(약사법) 차단 — 제목 기반. (blocked, matched_keyword).
    탐폰·생리대·염모제·손소독제·구강청결제·콘돔·보건용마스크 등.
    허가/수입자격 없이 구매대행 등록 불가. PA_DISABLE_QUASI_DRUG_BLOCK=1 로 해제."""
    import os as _os
    if _os.environ.get("PA_DISABLE_QUASI_DRUG_BLOCK", "").strip() in ("1", "true", "yes"):
        return False, None
    hay = f"{title_ko or ''} {title_en or ''}".lower()
    if not hay.strip():
        return False, None
    for ex in _QUASI_DRUG_FP_EXCLUDES:
        if ex.lower() in hay:
            return False, None
    for kw in QUASI_DRUG_KEYWORDS:
        if kw.lower() in hay:
            return True, kw
    if "마스크" in hay or "mask" in hay:
        for mk in _QUASI_DRUG_MASK_KEYWORDS:
            if mk.lower() in hay:
                return True, f"보건용마스크:{mk}"
    return False, None


# ── 전기용품 (KC 전기안전인증 필요 — 인증 없이 판매 위반) 차단 (2026-06-03) ──
# 전동 커피머신(Keurig/Nespresso/드립)·일반 가전 자동 제외. 영문 title 기준(소싱/promote).
# 수동·비전동(pour over/french press/stovetop 등)은 제외. PA_DISABLE_ELECTRIC_BLOCK=1 해제.
ELECTRIC_APPLIANCE_KEYWORDS = (
    # 전동 커피/그라인더
    "keurig", "nespresso", "k-cup", "k cup", "kcup", "k-mini", "k-elite",
    "k-express", "k-duo", "k-compact", "k-slim", "single serve", "single-serve",
    "drip coffee", "programmable", "espresso machine", "espresso maker",
    "coffee brewer", "velocity brew", "flexbrew", "pod coffee", "digital coffee",
    "burr grinder", "percolator", "coffee center", "barista bar", "mr. coffee",
    "mr coffee", "cup coffee maker", "cup brew", "brew switch",
    # 일반 전기용품
    "electric", "blender", "toaster", "air fryer", "airfryer", "microwave",
    "food processor", "juicer", "rice cooker", "slow cooker", "instant pot",
    "pressure cooker", "stand mixer", "hand mixer", "electric kettle",
    "waffle maker", "hot plate", "induction cooktop", "deep fryer",
)
_ELECTRIC_FP_EXCLUDES = (
    "pour over", "pour-over", "pourover", "french press", "stovetop",
    "stove top", "stove-top", "moka pot", "hand grinder", "manual grinder",
    "manual coffee",
)


def check_electric_appliance(
    title_en: Optional[str], title_ko: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """전기용품(KC 전기안전인증 대상) 차단. 영문 title 우선, 수동·비전동은 제외.
    PA_DISABLE_ELECTRIC_BLOCK=1 로 해제."""
    import os as _os
    if _os.environ.get("PA_DISABLE_ELECTRIC_BLOCK", "").strip() in ("1", "true", "yes"):
        return False, None
    text = ((title_en or "") + " " + (title_ko or "")).lower()
    if not text.strip():
        return False, None
    for ex in _ELECTRIC_FP_EXCLUDES:
        if ex in text:
            return False, None
    for kw in ELECTRIC_APPLIANCE_KEYWORDS:
        if kw in text:
            return True, kw
    return False, None


def has_efficacy_claims(text: str) -> bool:
    """효능 표현 포함 여부 (검출만, 수정 안 함)."""
    if not text:
        return False
    return bool(_EFFICACY_RE.search(text))


def sanitize_efficacy_claims(text: str) -> str:
    """효능 표현 제거. 매칭 부분을 빈 문자열로 치환 후 공백 정리."""
    if not text:
        return text
    cleaned = _EFFICACY_RE.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s*[,，]\s*[,，]\s*", ", ", cleaned)  # ", ," → ","
    cleaned = re.sub(r"^[,，\s]+|[,，\s]+$", "", cleaned)    # 앞뒤 콤마 제거
    return cleaned


# 해외직구 식품 searchTags 에서 추가로 떨어내는 기능성·효능·신체부위 키워드.
# (효능 패턴이 문장형이라 단일어 태그를 못 거르는 케이스 보강 — 식품표시광고법: 해외직구 식품 기능성 전면금지)
_EFFICACY_TAG_WORDS = frozenset({
    "피부", "모발", "손톱", "헤어", "스킨", "네일", "이너뷰티", "뷰티", "미용",
    "탄력", "주름", "미백", "보습", "흡수", "흡수율", "면역", "항산화", "관절",
    "두뇌", "혈행", "혈액순환", "다이어트", "체지방", "안티에이징", "노화",
    "피로", "활력", "디톡스", "해독", "혈당", "혈압", "콜레스테롤",
    "장건강", "눈건강", "간건강", "근력", "회복", "재생",
})


def filter_efficacy_tags(tags) -> list:
    """해외직구 식품 searchTags 에서 기능성·효능·신체부위 키워드 태그 제거.

    식품표시광고법 — 해외직구 식품은 기능성/효능 표현 전면 금지. 건강식품 카테고리에만 적용.
    효능 패턴(문장형) + 단일어 기능성 키워드 둘 다 검사. 성분명/제품명/규격은 보존.
    """
    out = []
    for t in (tags or []):
        s = str(t).strip()
        if not s:
            continue
        if has_efficacy_claims(s):
            continue
        if any(w in s for w in _EFFICACY_TAG_WORDS):
            continue
        out.append(s)
    return out


# 음료수류(마시는 RTD 음료) 제외용 — 구매대행 액상음료 배제 (2026-06-19, 단백질 쉐이크 RTD 등).
# ★모든 액체가 아니라 "마시는 음료/RTD"만. 파우더·믹스·분말·캡슐·오일은 음료 아님(통과).
_BEVERAGE_KEYWORDS = (
    "음료", "드링크", "주스", "juice", "에이드", "스무디", "smoothie", "콤부차", "kombucha",
    "탄산음료", "사이다", "식혜", "수정과", "마시는", "ready to drink",
    "이온음료", "에너지음료", "energy drink", "코코넛워터", "coconut water", "탄산수",
    "스파클링", "sparkling",
    # ★"콜라"/"cola" 제외(콜라겐·acerola 충돌), "라떼" 제외(라떼파우더 충돌), "생수" 제외(오탐방지)
    # → 콜라/생수류는 '생수/음료' 카테고리 경로로 잡힘
)
_BEVERAGE_POWDER_SIGNALS = (
    "파우더", "powder", "분말", "믹스", " mix", "lbs", "파운드", "회분", "정", "캡슐",
    "softgel", "소프트젤", "scoop", "스쿱",
)


def check_beverage(name: str, cat_path: str = "") -> tuple[bool, Optional[str]]:
    """음료수류(마시는 RTD 음료) 여부. True면 업로드 제외 대상.

    ①쿠팡 '생수/음료' 카테고리 ②명확한 음료 키워드 ③쉐이크/shake 는 RTD(액상)이고
    파우더/믹스/정/캡슐 신호가 없을 때만 음료로 판정(파우더 단백질은 통과).
    """
    text = (name or "").lower()
    path = cat_path or ""
    # ① 카테고리 '생수/음료' 트리 = 확정 RTD 음료 (파우더 신호 무관)
    if "생수/음료" in path or "> 음료 >" in path or path.endswith("> 음료"):
        return True, f"음료 카테고리({path[:40]})"
    # ② 파우더/믹스/분말/정/캡슐 신호 = '마시는 음료' 아님 → 통과
    #    (드링크믹스·프로틴파우더·정제·소프트젤 등은 RTD가 아니므로 제외 안 함)
    if any(s in text for s in _BEVERAGE_POWDER_SIGNALS):
        return False, None
    # ③ RTD 음료 키워드 / 쉐이크
    for kw in _BEVERAGE_KEYWORDS:
        if kw in text:
            return True, f"음료 키워드('{kw.strip()}')"
    if "쉐이크" in text or "shake" in text or "셰이크" in text:
        return True, "RTD 쉐이크(액상)"
    return False, None


def check_duplicate_asin(
    asin: str,
    channel: str,
    exclude_product_id: int,
    coupang_account: Optional[str] = None,
) -> tuple[bool, Optional[dict]]:
    """같은 ASIN 이 다른 product_id 로 이미 listed 상태로 등록돼 있는지 검사.

    coupang_account 지정 시 해당 계정 listing 만 중복으로 본다 (멀티계정 — 구계정의
    같은 ASIN listing 이 신계정 등록을 막던 버그 방지). None=계정 무관(기존 동작).

    Returns:
        (True, {'product_id': ..., 'channel_product_id': ...}) — 중복
        (False, None) — 통과
    """
    if not asin:
        return False, None
    sql = """SELECT l.product_id, l.channel_product_id
               FROM listings_pa l
               JOIN products p ON l.product_id = p.id
               WHERE p.asin = ?
                 AND l.channel = ?
                 AND l.status = 'listed'
                 AND l.product_id != ?"""
    params = [asin, channel, exclude_product_id]
    if coupang_account:
        sql += " AND l.coupang_account = ?"
        params.append(coupang_account)
    sql += " LIMIT 1"
    with get_db() as conn:
        row = conn.execute(sql, params).fetchone()
    if row:
        return True, dict(row)
    return False, None


def check_korean_manufacturer(mfr: Optional[str]) -> tuple[bool, str]:
    """한국 manufacturer 차단 게이트 — IP 라이선스 보호 (사전 예방).

    동작:
        1. mfr 빈 값 → (False, 'no_manufacturer')   # 차단 불가, 통과
        2. DB 조회 manufacturer_is_korean
           - 1 → (True, 'cached_korean')             # 즉시 차단
           - 0 → (False, 'cached_not_korean')        # 즉시 통과
        3. 미분류(NULL) → manufacturer_classifier.classify_korean_sync()
           - is_korean=True  → DB UPDATE + (True, 'fresh_korean: <reason>')
           - is_korean=False → DB UPDATE + (False, 'fresh_not_korean: <reason>')
           - 분류 실패       → (False, 'classify_failed')   # 보수적 통과; 사후 daemon 이 잡음

    Returns:
        (blocked, reason) — blocked=True 면 호출자가 log_violation 후 차단 처리.

    동시성:
        같은 mfr 을 일괄 batch (`classify_mfr_with_search.py`) 가 동시에 분류 중일 수 있음.
        idempotent UPDATE 로 race 무해. busy_timeout 으로 락 회피.
    """
    if not mfr or not str(mfr).strip():
        return False, "no_manufacturer"

    mfr_norm = str(mfr).strip()
    if mfr_norm == "__NONE__":
        return False, "no_manufacturer"

    # ── 1. DB 캐시 조회 ──
    try:
        with get_db() as conn:
            row = conn.execute(
                """SELECT manufacturer_is_korean FROM products
                   WHERE amazon_manufacturer=? AND manufacturer_is_korean IS NOT NULL
                   LIMIT 1""",
                (mfr_norm,),
            ).fetchone()
            if row is None:
                # amazon_manufacturer 는 21.1% 만 채워져 있다(2026-08-05 실측).
                # sp_manufacturer(88.3%) 로 재조회 — 쿼리를 나눠 각 인덱스를 살린다.
                row = conn.execute(
                    """SELECT manufacturer_is_korean FROM products
                       WHERE sp_manufacturer=? AND manufacturer_is_korean IS NOT NULL
                       LIMIT 1""",
                    (mfr_norm,),
                ).fetchone()
        if row is not None:
            is_korean = row["manufacturer_is_korean"]
            if is_korean == 1:
                return True, "cached_korean"
            return False, "cached_not_korean"
    except Exception as e:
        logger.warning(f"[clean_policy] mfr 캐시 조회 실패 ({mfr_norm}): {e}")
        # 조회 실패 시 분류 시도로 진행

    # ── 2. 미분류 — 인라인 classify ──
    try:
        from backend.purchase.services import manufacturer_classifier
        result = manufacturer_classifier.classify_korean_sync(mfr_norm)
    except Exception as e:
        logger.warning(f"[clean_policy] mfr classify 호출 예외 ({mfr_norm}): {e}")
        return False, "classify_failed"

    if not result or "is_korean" not in result:
        return False, "classify_failed"

    is_korean_bool = bool(result.get("is_korean"))
    is_korean_int = 1 if is_korean_bool else 0
    reason_short = (result.get("reason") or "")[:30]

    # ── 3. DB 캐시 갱신 (idempotent) ──
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for retry in range(5):
        try:
            with get_db() as conn:
                conn.execute(
                    """UPDATE products
                       SET manufacturer_is_korean=?, manufacturer_classified_at=?
                       WHERE amazon_manufacturer=?""",
                    (is_korean_int, now_iso, mfr_norm),
                )
                # amazon 이 빈 행은 sp_manufacturer 로 매칭해 함께 갱신한다.
                conn.execute(
                    """UPDATE products
                       SET manufacturer_is_korean=?, manufacturer_classified_at=?
                       WHERE sp_manufacturer=?
                         AND TRIM(COALESCE(amazon_manufacturer,''))=''""",
                    (is_korean_int, now_iso, mfr_norm),
                )
            break
        except Exception as e:
            if "lock" in str(e).lower() and retry < 4:
                import time as _time
                _time.sleep(2 + retry * 3)
                continue
            logger.warning(f"[clean_policy] mfr 캐시 UPDATE 실패 ({mfr_norm}): {e}")
            break

    if is_korean_bool:
        return True, f"fresh_korean: {reason_short}"
    return False, f"fresh_not_korean: {reason_short}"


def ensure_overseas_tag(name: str, max_len: int = 50) -> str:
    """상품명 앞에 [해외] 태그를 자동 부여. 50자 제한 유지.

    이미 [해외] 또는 유사 태그가 있으면 그대로 반환.
    """
    if not name:
        return name
    name = name.strip()
    # 이미 해외 관련 태그가 있는지 확인
    if re.match(r"^\s*\[(해외|구매대행|직배송|병행수입)\]", name):
        return name[:max_len]
    tag = "[해외] "
    available = max_len - len(tag)
    if available <= 0:
        return name[:max_len]
    return tag + name[:available]


_NUMERIC_UNIT_RE = re.compile(r"^\s*\d+\.?\d*\s*[a-zA-Z가-힣%]+\s*$")


def validate_numeric_attribute(value: str) -> str:
    """범위형 속성 값에서 단위/텍스트 제거. 숫자(.) 만 남김.

    "100ml" → "100"
    "250 g" → "250"
    "ABC" → "" (모두 제거되면 빈 문자열)
    "100" → "100" (변경 없음)
    """
    if not value:
        return value
    value = str(value).strip()
    if not _NUMERIC_UNIT_RE.match(value):
        return value  # 숫자+단위 패턴이 아니면 손대지 않음
    cleaned = re.sub(r"[^0-9.]", "", value)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)  # ".." → "."
    cleaned = cleaned.strip(".")
    return cleaned


def sanitize_attribute_dict(attr_dict: dict) -> dict:
    """단일 attribute dict 의 attributeValue 정제.

    네이버 inferred_attributes_json 형식: {'attributeSeq': N, 'attributeValueSeq': M, 'attributeValue': '...'}
    """
    if not isinstance(attr_dict, dict):
        return attr_dict
    val = attr_dict.get("attributeValue")
    if isinstance(val, str):
        cleaned = validate_numeric_attribute(val)
        if cleaned != val:
            attr_dict = {**attr_dict, "attributeValue": cleaned}
    return attr_dict


def check_naver_category_gate(amazon_category_json, naver_account: str = "new"):
    """네이버 등록 허용 여부 — (allowed: bool, reason: str).

    2026-08-03 사장 방침: 네이버 신계정 = 화장품 전용 / 구계정 = 식품·건기식.
      실측 배경 — 첫 업로드에서 나사·가발·마사지건 등이 섞여 550개 중 163개(30%)가 부적합.
      경로는 두 갈래였다: ①배정 파일 오염 19건 ②그룹 등록이 부모의 미배정 형제 144건을 끌고옴.
      ②가 증폭 요인이라 배정만 고쳐서는 재발한다 → 등록 직전에 차단한다.
    카테고리 정보가 없으면 차단(보수적) — 쿠팡으로 보내면 되므로 손실이 없다.
    """
    cls = classify_target_channel(amazon_category_json)
    acct = (naver_account or "new").lower()
    if acct == "new":
        if cls == "cosmetic":
            return True, ""
        return False, f"네이버 신계정은 화장품 전용 (판정={cls})"
    if cls in ("food", "cosmetic"):
        return True, ""
    return False, f"네이버 구계정은 식품·건기식 전용 (판정={cls})"


# ── 네이버 채널 게이트용 SP-API 기반 분류 (2026-08-03) ──────────────
#   amazon_category_json 이 실측 0% 채움이라 브레드크럼만으로는 판정 불가.
#   실제로 채워지는 필드: sp_product_type / sp_browse_classification / sp_website_display_group
_SP_COSMETIC_TYPES = {
    "SHAMPOO", "CONDITIONER", "HAIR_CLEANER_CONDITIONER", "MAKE_UP", "LUXURY_BEAUTY",
    "SKIN_CARE_AGENT", "SKIN_CLEANING_AGENT", "TOPICAL_HAIR_REGROWTH_TREATMENT",
    "HAIR_STYLING_AGENT", "HAIR_COLORING_AGENT", "BODY_DEODORANT", "SOAP",
    "FACIAL_TREATMENT", "SUNSCREEN", "NAIL_POLISH", "FRAGRANCE", "BATH_OIL",
    "HAIR_TREATMENT", "COSMETIC_BRUSH", "COSMETIC_CASE", "COSMETIC_POWDER",
}
_SP_FOOD_TYPES = {
    "SUPPLEMENT", "VITAMIN", "HERBAL_SUPPLEMENT", "PROTEIN_SUPPLEMENT_POWDER",
    "FOOD_SEASONING", "SNACK_FOOD", "BEVERAGE", "COFFEE", "TEA", "GROCERY",
    "CANDY", "NUT_AND_SEED", "DRIED_FRUIT", "EDIBLE_OIL", "HONEY", "CEREAL",
    "MEAL_REPLACEMENT_DRINK", "NUTRITIONAL_SUPPLEMENT",
}
_SP_COSMETIC_BROWSE = (
    "shampoo", "conditioner", "scalp", "hair", "makeup", "cosmetic", "skin",
    "moisturizer", "serum", "cleanser", "sunscreen", "nail", "fragrance",
    "beauty", "lotion", "toner", "mask",
)
_SP_FOOD_BROWSE = (
    "supplement", "vitamin", "grocery", "food", "snack", "beverage", "coffee",
    "tea", "protein", "nutrition", "candy", "seasoning",
)


# 제목 보조 판정 — SP 필드가 전부 비어 있는 상품이 실측 29%(127건 중 37건).
#   그 대부분이 샴푸/컨디셔너였다. 제목은 AI 가공으로 항상 채워지므로 최후 근거로 쓴다.
_TITLE_COSMETIC = (
    # ★한글은 다른 단어에 박히는 짧은 어근을 뺐다. 실측 오탐:
    #   "그립 트레이너"→'립' / "가발"→'헤어' / "도시락 가방"→'크림'
    "샴푸", "컨디셔너", "트리트먼트", "비듬", "두피",
    "메이크업", "화장품", "코스메틱", "파운데이션", "아이섀도", "마스카라",
    "선크림", "선스크린", "자외선차단", "스킨케어", "에센스", "세럼", "앰플",
    "토너", "클렌징", "클렌저", "마스크팩", "매니큐어", "네일폴리시",
    "향수", "퍼퓸", "바디워시", "바디로션", "핸드크림", "립스틱", "립밤",
    "헤어오일", "헤어에센스", "헤어팩", "염색약", "탈모",
)
_TITLE_COSMETIC_EN = (
    "shampoo", "conditioner", "hair care", "scalp", "makeup", "cosmetic",
    "serum", "moisturizer", "cleanser", "sunscreen", "nail polish",
    "lipstick", "mascara", "perfume", "body wash", "dandruff",
)
_TITLE_FOOD = (
    "비타민", "영양제", "보충제", "유산균", "프로바이오틱", "오메가", "콜라겐",
    "프로틴", "홍삼", "루테인", "밀크씨슬", "마그네슘", "아연",
)
_TITLE_FOOD_EN = (
    "vitamin", "supplement", "probiotic", "omega", "collagen", "protein powder",
    "lutein", "milk thistle", "magnesium",
)


def _classify_by_title(title_ko=None, title_en=None) -> str:
    ko = (title_ko or "")
    en = (title_en or "").lower()
    if any(k in ko for k in _TITLE_COSMETIC) or any(k in en for k in _TITLE_COSMETIC_EN):
        return "cosmetic"
    if any(k in ko for k in _TITLE_FOOD) or any(k in en for k in _TITLE_FOOD_EN):
        return "food"
    return "general"


def classify_channel_by_sp(product_type=None, browse=None, display_group=None,
                           amazon_category_json=None, title_ko=None, title_en=None) -> str:
    """'cosmetic' | 'food' | 'general' — SP-API 필드 우선, 브레드크럼 보조."""
    pt = (product_type or "").strip().upper()
    if pt in _SP_COSMETIC_TYPES:
        return "cosmetic"
    if pt in _SP_FOOD_TYPES:
        return "food"
    b = (browse or "").strip().lower()
    if b:
        if any(k in b for k in _SP_COSMETIC_BROWSE):
            return "cosmetic"
        if any(k in b for k in _SP_FOOD_BROWSE):
            return "food"
    dg = (display_group or "").strip().lower()
    if dg in ("beauty", "prestige beauty", "luxury beauty"):
        return "cosmetic"
    if dg in ("grocery", "gourmet food", "health and beauty"):
        return "food"
    # 브레드크럼 → 마지막으로 제목
    cls = classify_target_channel(amazon_category_json)
    if cls != "general":
        return cls
    return _classify_by_title(title_ko, title_en)


def check_naver_gate_by_product(row, naver_account: str = "new"):
    """products row(dict-like) 로 네이버 등록 허용 판정 — (allowed, reason, cls)."""
    def _g(k):
        try:
            return row[k]
        except Exception:
            return None
    cls = classify_channel_by_sp(_g("sp_product_type"), _g("sp_browse_classification"),
                                 _g("sp_website_display_group"), _g("amazon_category_json"),
                                 _g("title_ko"), _g("title_en"))
    acct = (naver_account or "new").lower()
    if acct == "new":
        return (cls == "cosmetic", "" if cls == "cosmetic"
                else f"네이버 신계정은 화장품 전용 (판정={cls})", cls)
    return (cls in ("food", "cosmetic"), "" if cls in ("food", "cosmetic")
            else f"네이버 구계정은 식품·건기식 전용 (판정={cls})", cls)
