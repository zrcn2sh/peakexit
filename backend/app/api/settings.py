from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from app.core.state import get_settings, save_settings

router = APIRouter()


class SettingsUpdate(BaseModel):
    stop_loss_pct: Optional[float] = None          # 예: -10.0
    trailing_trigger_pct: Optional[float] = None   # 예: 5.0
    trailing_drop_pct: Optional[float] = None      # 예: 5.0
    take_profit_pct: Optional[float] = None        # 예: 30.0  (None=비활성화)
    seed_money: Optional[float] = None             # 예: 1000000
    enabled: Optional[bool] = None
    check_interval_minutes: Optional[int] = None


@router.get("/")
def get_settings_api():
    return get_settings()


@router.patch("/")
def update_settings(body: SettingsUpdate):
    current = get_settings()
    updates = body.model_dump(exclude_none=True)
    current.update(updates)
    save_settings(current)
    return current
