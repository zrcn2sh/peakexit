from fastapi import APIRouter
from app.core.state import get_sell_log
from app.core.sell_engine import enrich_sell_log_with_live_prices

router = APIRouter()


@router.get("/sell-log")
def get_sell_log_api():
    """매도 이력 조회 (금액 보강 + 매도 후 현재가·등락)."""
    logs = get_sell_log()
    return enrich_sell_log_with_live_prices(logs)
