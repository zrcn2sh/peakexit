from fastapi import APIRouter
from app.core.state import get_sell_log

router = APIRouter()


@router.get("/sell-log")
def get_sell_log_api():
    """매도 이력 조회"""
    return get_sell_log()
