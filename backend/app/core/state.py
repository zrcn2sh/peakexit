"""
앱 전역 상태: 설정값, 매도 로그
파일 기반 영속 저장 (PEAKEXIT_DATA_DIR 또는 OS별 기본 경로)
"""
import json
from pathlib import Path

from app.core.paths import get_data_dir


def _settings_path() -> Path:
    return get_data_dir() / "settings.json"


def _sell_log_path() -> Path:
    return get_data_dir() / "sell_log.json"

DEFAULT_SETTINGS = {
    "stop_loss_pct": -10.0,
    "trailing_trigger_pct": 5.0,
    "trailing_drop_pct": 5.0,
    "take_profit_pct": None,
    "seed_money": 1_000_000,
    "enabled": True,
    "check_interval_minutes": 5,
}


def _ensure_dir():
    get_data_dir().mkdir(parents=True, exist_ok=True)


def get_settings() -> dict:
    _ensure_dir()
    path = _settings_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()


def save_settings(settings: dict):
    _ensure_dir()
    _settings_path().write_text(json.dumps(settings, ensure_ascii=False, indent=2))


def get_sell_log() -> list:
    _ensure_dir()
    path = _sell_log_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return []


def append_sell_log(entry: dict):
    _ensure_dir()
    logs = get_sell_log()
    logs.insert(0, entry)
    logs = logs[:200]  # 최근 200건만 보관
    _sell_log_path().write_text(json.dumps(logs, ensure_ascii=False, indent=2))
