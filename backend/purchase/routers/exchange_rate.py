"""USD/KRW 환율 조회/갱신 라우터."""
from fastapi import APIRouter, Depends, HTTPException

from backend.purchase.auth import current_user
from backend.purchase.services import exchange_rate_service as svc

router = APIRouter(prefix="/api/pa/exchange-rate", tags=["pa-exchange-rate"])


@router.get("")
def get_current(user: dict = Depends(current_user)):
    return {
        "rate": svc.get_current_rate(),
        "updated_at": svc.get_updated_at(),
    }


@router.post("/refresh")
def refresh(user: dict = Depends(current_user)):
    try:
        return svc.update_and_store()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
