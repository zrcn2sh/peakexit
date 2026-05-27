"""
USD→KRW 환율 (시드 대비·합산 평가용)

- 1시간 메모리 캐시
- 환경변수 FX_USD_KRW 가 있으면 API 대신 해당 값 사용 (비상/오프라인)
- 공개 API 순차 시도 (키 불필요)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

_cache: Optional[tuple[float, float]] = None  # (rate, unix_ts)
_CACHE_TTL_SEC = 3600


def _parse_er_api(data: dict) -> Optional[float]:
    if data.get("result") != "success":
        return None
    rates = data.get("conversion_rates") or data.get("rates") or {}
    v = rates.get("KRW")
    if v is None:
        return None
    return float(v)


def _parse_exchangerate_host(data: dict) -> Optional[float]:
    rates = data.get("rates") or {}
    v = rates.get("KRW")
    if v is None:
        return None
    return float(v)


def _fetch_one(url: str, parser: Callable[[dict], Optional[float]], timeout: float) -> Optional[float]:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return parser(resp.json())


def get_usd_krw_rate(timeout: float = 6.0) -> Optional[float]:
    """
    1 USD당 KRW. 실패 시 캐시된 값이 있으면 캐시 반환, 없으면 None.
    """
    global _cache
    env = os.getenv("FX_USD_KRW", "").strip()
    if env:
        try:
            v = float(env)
            if v > 0:
                return v
        except ValueError:
            logger.warning("FX_USD_KRW 값이 올바른 숫자가 아닙니다. 무시합니다.")

    now = time.time()
    if _cache and (now - _cache[1]) < _CACHE_TTL_SEC:
        return _cache[0]

    endpoints: list[tuple[str, Callable[[dict], Optional[float]]]] = [
        ("https://open.er-api.com/v6/latest/USD", _parse_er_api),
        ("https://api.exchangerate.host/latest?base=USD&symbols=KRW", _parse_exchangerate_host),
    ]
    last_err: Optional[Exception] = None
    for url, parser in endpoints:
        try:
            rate = _fetch_one(url, parser, timeout=timeout)
            if rate and rate > 500:  # 비정상 값 필터 (대략 2020년대 환율대)
                _cache = (rate, now)
                logger.info("USD/KRW 환율 조회: %.2f (%s)", rate, url.split("/")[2])
                return rate
        except Exception as e:
            last_err = e
            continue

    if _cache:
        logger.warning("USD/KRW 환율 API 실패, 캐시 사용: %s (오류: %s)", _cache[0], last_err)
        return _cache[0]
    logger.error("USD/KRW 환율을 가져오지 못했습니다: %s", last_err)
    return None
