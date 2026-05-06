"""직배 불가 상품 배대지 경유 가격 재산정 라우터.

업로드 파이프라인 무관 — listings_pa 의 가격/메타만 변경.
채널 sync (스마트스토어/쿠팡 실제 가격 변경) 는 별도 endpoint.
"""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.purchase.auth import current_user
from backend.purchase.services.forwarder_pricing import (
    recalculate_blocked_listings, summary, apply_exclusions,
)

router = APIRouter(prefix="/api/pa/forwarder-pricing", tags=["pa-forwarder-pricing"])


class RecalcBody(BaseModel):
    apply: bool = True   # False = dry-run (분류 결과만)
    channel: Optional[str] = None
    limit: Optional[int] = Field(None, ge=1, le=20000)


@router.post("/recalculate")
def forwarder_recalculate(body: RecalcBody, user: dict = Depends(current_user)):
    """kr_shipping_eligible=0 인 listings 일괄 재산정 + 자동 분류 + DB 적용.

    - apply=True: sale_krw 변경 + 메타 채움
    - apply=False: dry-run (분류 결과만 반환, DB 변경 X)
    """
    return recalculate_blocked_listings(
        apply=body.apply, channel=body.channel, limit=body.limit,
    )


@router.get("/summary")
def forwarder_summary(user: dict = Depends(current_user)):
    return summary()


class ApplyExclusionsBody(BaseModel):
    channel: Optional[str] = None
    limit: Optional[int] = Field(None, ge=1, le=20000)


@router.post("/apply-exclusions")
def forwarder_apply_exclusions(body: ApplyExclusionsBody, user: dict = Depends(current_user)):
    """forwarder_action='mark_exclude' 상품들의 status='excluded' 일괄 변경.

    Destructive — 채널 검색에서 사라짐. 사용자가 명시 트리거.
    """
    return apply_exclusions(channel=body.channel, limit=body.limit)
