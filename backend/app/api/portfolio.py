from fastapi import APIRouter
from app.core.state import get_sell_log
from app.core.sell_engine import enrich_sell_execution_details

router = APIRouter()


@router.get("/sell-log")
def get_sell_log_api():
    """매도 이력 조회 (표시용 금액 필드 보강)"""
    logs = get_sell_log()
    return [enrich_sell_execution_details(entry) for entry in logs]
