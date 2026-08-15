# -*- coding: utf-8 -*-
"""리스팅 상태의 정본. 2026-08-15

★여기서만 정의한다. 라우터·잡·스크립트는 전부 import 해서 쓴다.
  손으로 다시 적으면 반드시 갈린다 — 하루에 두 번 겪었다.

DEAD     채널에서 내려간 상태. '살아있는 리스팅' 집계에서 뺀다.
SELLING  채널 원문 중 '지금 팔리고 있다'는 값. ★우리 status 가 아니라 channel_status 를 본다.
"""

DEAD = (
    "removed", "excluded", "archived", "deleted", "stopped",
    "failed", "error", "rejected", "cancelled",
    # 네이버 한도회전으로 delete_product 한 것. 판매중지가 아니라 영구삭제다.
    "rotated",
)

# 채널 원문. 채널마다 말이 다르다 — 번역하지 않고 그대로 비교한다.
SELLING = ("SALE", "판매중", "승인완료", "OnSale")


def dead_sql(col="status"):
    """`status NOT IN (...)` 를 만든다. 파라미터 바인딩용 값도 같이 돌려준다."""
    return "%s NOT IN (%s)" % (col, ",".join("?" * len(DEAD))), list(DEAD)


def selling_sql(col="channel_status"):
    return "%s IN (%s)" % (col, ",".join("?" * len(SELLING))), list(SELLING)
