"""브랜드 게이팅 필터 검증 — 정탐(차단돼야) + 오탐(통과돼야) 케이스."""
from backend.purchase.services import clean_policy
cases = [
    # 정탐 (차단 기대)
    ("Nike Air Max 270 Running Shoes", "", True),
    ("Bose QuietComfort 45 Headphones", "", True),
    ("Braun Series 9 Pro Shaver", "", True),
    ("The Ordinary Niacinamide 10% Serum", "", True),
    ("ASICS Gel-Kayano 30", "", True),
    ("New Balance 990v6 Sneakers", "", True),
    ("Logitech MX Master 3S Mouse", "", True),
    ("Cetaphil Gentle Skin Cleanser 16oz", "", True),
    ("Lancome La Vie Est Belle EDP", "", True),
    ("Estee Lauder Advanced Night Repair", "", True),
    ("Under Armour Tech 2.0 T-Shirt", "", True),
    ("JBL Flip 6 Speaker", "", True),
    ("Le Creuset Dutch Oven 5.5qt", "", True),
    ("", "나이키 에어맥스 운동화", True),       # 한글 title 매칭
    # 오탐 검증 (통과 기대 — 차단되면 안 됨)
    ("Brown Leather Wallet for Men", "브라운 가죽 지갑", False),
    ("Boston Terrier Plush Dog Toy", "보스턴 테리어 인형", False),
    ("Boss Audio Systems Car Speaker", "보스 오디오 스피커", False),
    ("Cappuccino Coffee Maker Machine", "카푸치노 메이커", False),
    ("Stainless Steel Insulated Water Bottle", "스테인리스 물병", False),
    ("Ordinary Household Cleaning Spray", "", False),   # 'ordinary' 단독 (The 없음)
]
bad = 0
for en, ko, expect in cases:
    blocked, kw = clean_policy.check_prohibited_ingredients(en, ko)
    ok = (blocked == expect)
    if not ok:
        bad += 1
    print(f"  [{'OK' if ok else '‼MISMATCH'}] expect={str(expect):5s} got={str(blocked):5s} kw={kw!r} | {(en or ko)[:45]}")
print(f"\n결과: {len(cases)-bad}/{len(cases)} 통과, 불일치 {bad}건")
