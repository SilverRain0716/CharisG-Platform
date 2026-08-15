"""판매처(채널 × 계정) 레지스트리 조회.

콘솔 셸이 채널 탭과 계정 줄을 그리려면 "어떤 채널이 있고, 각 채널에 어떤
계정이 붙어 있으며, 지금 쓸 수 있는 상태인가"를 알아야 한다. 그 단일 출처가
`seller_accounts` 테이블이다(cold.db, 5플랫폼 × 2사업자 = 10행).

★프런트에 채널 목록을 하드코딩하지 말 것. 11번가 두 번째 계정이 붙거나 ESM
  API 가 열리면 이 테이블 한 줄만 바뀌고 화면은 그대로 따라와야 한다.

노출하지 않는 것: 자격증명. 이 테이블에는 원래 키가 없지만(키는 .env),
필드를 통째로 흘리지 않고 화면이 쓰는 것만 골라 내보낸다.
"""
from fastapi import APIRouter, Depends

from backend.purchase.auth import current_user
from backend.purchase.database import get_db

router = APIRouter(prefix="/api/pa", tags=["pa-accounts"])

# 화면에 쓰는 표시 정보. platform 코드는 DB 값 그대로 쓰고, 라벨과 문자 마크만
# 여기서 붙인다. 마크는 색약 조건에서 색만으로 채널을 구분하지 못하기 때문에 필요하다.
PLATFORM_META = {
    "coupang":    {"label": "쿠팡",         "mark": "C",  "order": 1},
    "smartstore": {"label": "스마트스토어", "mark": "N",  "order": 2},
    "elevenst":   {"label": "11번가",       "mark": "11", "order": 3},
    # ★옥션이 G마켓보다 앞이다 — 옥션은 API 가 열려 실제로 등록하고,
    #   G마켓은 아직 연동 수단이 없다(탭 잠김). 쓰는 채널이 앞에 와야 한다.
    "auction":    {"label": "옥션",         "mark": "A",  "order": 4},
    "gmarket":    {"label": "G마켓",        "mark": "G",  "order": 5},
}

# ★2026-08-15 옥션을 ESM 묶음에서 뺐다.
#   묶은 전제는 "ESM 은 셀러용 공개 API 가 없다"였는데, 옥션 SOAP API 가 신·구 계정 모두
#   열리면서 깨졌다. 무엇보다 listings_pa.channel 에는 **auction** 으로 저장되는데
#   화면 키가 'esm' 이라 키가 안 맞아 **옥션 리스팅이 어느 화면에도 안 나왔다**(실측 4건).
#   G마켓은 아직 자격증명·API 가 없어 남겨 둔다 — 열리면 여기서 빼면 된다.
# ★G마켓도 뺐다 — 묶음에 하나만 남으면 'ESM' 이라는 이름이 오히려 정체를 가린다.
#   지금은 묶을 이유가 없다. G마켓 API 가 열리면 그냥 자기 이름으로 뜬다.
ESM_PLATFORMS = ()

# 계정을 실제로 쓸 수 있는 상태. pending(자격 신청중)·unknown(연동 수단 없음)은
# 진입시켜도 빈 화면만 나오므로 탭에서 잠근다.
#
# ★status 는 **연동 능력**만 말한다. 재고가 있는지는 inventory 가 따로 말한다.
#   2026-08-15 이전에는 네이버 두 계정이 'wiped'(전량삭제 이력) 라서 탭이 잠겼는데,
#   정작 그 계정에서 상품이 **실제로 팔리고 있었다**(13406789937 · SALE · 160,100원).
#   파는 물건을 화면에서 볼 수 없는 상태였다. 이력과 능력을 섞으면 이렇게 된다.
USABLE_STATUS = ("active", "ready", "reducing")

# 재고 상태 — 탭 잠금과 무관하다. 화면 배지로만 쓴다.
#   live 운영중 · rebuilding 재구축중 · wiped 비움 · empty 등록한 적 없음
INVENTORY_LABEL = {
    "live": "운영중", "rebuilding": "재구축중", "wiped": "비움", "empty": "미등록",
}


@router.get("/accounts")
def list_accounts(user: dict = Depends(current_user)):
    """채널 → 계정 목록. 셸이 이 응답 하나로 탭·계정 줄·잠금 상태를 전부 그린다."""
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, platform, business_entity, account_key, credential_group, "
            "       channel_id, store_name, vendor_id, fee_rate, "
            "       limit_products, limit_daily, status, inventory, note "
            "FROM seller_accounts"
        ).fetchall()]

    by_channel: dict[str, dict] = {}
    for r in rows:
        platform = r["platform"]
        # 옥션·G마켓은 같은 자격증명을 쓰는 한 덩어리라 하나의 채널로 묶는다.
        channel = "esm" if platform in ESM_PLATFORMS else platform
        meta = PLATFORM_META.get(platform, {"label": platform, "mark": platform[:2].upper(), "order": 99})

        ch = by_channel.setdefault(channel, {
            "channel": channel,
            "label": "ESM" if channel == "esm" else meta["label"],
            "mark": "E" if channel == "esm" else meta["mark"],
            "order": 4 if channel == "esm" else meta["order"],
            "accounts": [],
        })

        # ESM 은 옥션·G마켓이 같은 자격증명(credential_group)을 쓰는 한 덩어리다.
        # 계정 줄에 '카리스G'가 두 번 뜨면 무엇이 다른지 알 수 없으므로, 계정은
        # 신·구 둘로 접고 마켓은 그 안의 목록으로 내린다.
        existing = next((a for a in ch["accounts"] if a["account_key"] == r["account_key"]), None)
        if existing is not None:
            existing["markets"].append(meta["label"])
            # 한쪽이라도 쓸 수 있으면 계정은 쓸 수 있다
            if r["status"] in USABLE_STATUS:
                existing["usable"] = True
                existing["status"] = r["status"]
            continue

        ch["accounts"].append({
            "id": r["id"],
            "platform": platform,
            "markets": [meta["label"]],          # ESM 안에서 옥션/G마켓 구분용
            "account_key": r["account_key"],     # old | new
            "entity": r["business_entity"],      # charisg | charisglobal
            "label": "카리스G" if r["business_entity"] == "charisg" else "카리스 글로벌",
            "store_name": r["store_name"],
            "vendor_id": r["vendor_id"],
            "fee_rate": r["fee_rate"],
            "limit_products": r["limit_products"],
            "limit_daily": r["limit_daily"],
            "status": r["status"],
            "inventory": r["inventory"],
            "inventory_label": INVENTORY_LABEL.get(r["inventory"] or "", ""),
            "usable": r["status"] in USABLE_STATUS,
            "note": r["note"],
        })

    channels = sorted(by_channel.values(), key=lambda c: c["order"])
    for ch in channels:
        # 계정 줄 순서는 항상 신 → 구. 운영 중인 쪽이 먼저 와야 한다.
        ch["accounts"].sort(key=lambda a: (a["account_key"] != "new", a["platform"]))
        # 채널 자체가 쓸 수 있는가 = 계정 중 하나라도 쓸 수 있는가
        ch["usable"] = any(a["usable"] for a in ch["accounts"])

    return {
        "channels": channels,
        # 합계는 행 수가 아니라 화면이 다루는 컨텍스트 수로 센다(ESM 4행 = 계정 2개).
        "total_accounts": sum(len(c["accounts"]) for c in channels),
        "usable_accounts": sum(1 for c in channels for a in c["accounts"] if a["usable"]),
    }
