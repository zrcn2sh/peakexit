"""
트레일링 비율 자동 결정 서비스 (ATR 기반)

흐름:
1. 한투 API → 14일 일봉 → ATR 계산
2. 종목 분류 (한국/미국, 대형/ETF/레버리지 등)
3. ATR × 2.5배로 트레일링 % 자동 결정 (AI 불필요)
4. trailing_config.json 캐싱 (주 1회 갱신, 경로는 app.core.paths.get_data_dir)
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.core.paths import get_data_dir

logger = logging.getLogger(__name__)


def _trailing_config_path() -> Path:
    return get_data_dir() / "trailing_config.json"
CACHE_TTL_HOURS = 24 * 7  # 주 1회 갱신

# ── 레버리지 ETF 티커 목록 ──────────────────────────
LEVERAGED_ETFS = {
    "SOXL","TQQQ","UPRO","SPXL","LABU","FNGU","TECL",
    "NAIL","DFEN","WANT","HIBL","UDOW","MIDU","URTY",
    "TNA","CURE","DPST","WEBL","RETL",
}

BROAD_INDEX_ETFS = {
    "SPY","QQQ","IWM","DIA","VTI","VOO","SCHD",
    "VNQ","BND","GLD","SLV","TLT","IEF","AGG",
}

KR_ETF_PREFIXES = ("KODEX","TIGER","KINDEX","ARIRANG","HANARO","KOSEF","KBSTAR")

# ── 분류별 기본값 (ATR 데이터 없을 때 폴백) ──────────
FALLBACK_TRAILING = {
    "레버리지ETF": (25.0, 10.0),
    "인버스ETF":   (20.0,  8.0),
    "지수ETF":     ( 4.0,  3.0),
    "섹터ETF":     ( 8.0,  5.0),
    "MegaCap":    ( 5.0,  4.0),
    "LargeCap":   ( 7.0,  5.0),
    "MidCap":     (10.0,  6.0),
    "SmallCap":   (15.0,  7.0),
    "대형주":      ( 6.0,  4.0),
    "중형주":      (10.0,  6.0),
    "소형주":      (15.0,  7.0),
    "코스닥":      (12.0,  6.0),
}

# ── 분류별 ATR 배수 ──────────────────────────────────
# 변동성이 높을수록 배수를 낮춰 과도한 트레일링 방지
ATR_MULTIPLIER = {
    "레버리지ETF": 2.0,   # ATR 자체가 이미 크므로 낮은 배수
    "인버스ETF":   2.0,
    "지수ETF":     3.0,   # 안정적이므로 높은 배수 허용
    "섹터ETF":     2.5,
    "MegaCap":    2.5,
    "LargeCap":   2.5,
    "MidCap":     2.5,
    "SmallCap":   2.0,
    "대형주":      2.5,
    "중형주":      2.5,
    "소형주":      2.0,
    "코스닥":      2.0,
}

# ── 분류별 트레일링 상한/하한 클램프 ────────────────
ATR_CLAMP = {
    "레버리지ETF": (15.0, 35.0),
    "인버스ETF":   (12.0, 30.0),
    "지수ETF":     ( 3.0,  8.0),
    "섹터ETF":     ( 5.0, 15.0),
    "MegaCap":    ( 4.0, 10.0),
    "LargeCap":   ( 5.0, 12.0),
    "MidCap":     ( 7.0, 18.0),
    "SmallCap":   (10.0, 25.0),
    "대형주":      ( 4.0, 10.0),
    "중형주":      ( 7.0, 15.0),
    "소형주":      (10.0, 20.0),
    "코스닥":      ( 8.0, 20.0),
}


# ─────────────────────────────────────────────────────
# 캐시 관리
# ─────────────────────────────────────────────────────
def _load_cache() -> dict:
    p = _trailing_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _save_cache(data: dict):
    _trailing_config_path().write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _is_cache_fresh(entry: dict) -> bool:
    updated = entry.get("updated_at")
    if not updated:
        return False
    diff = datetime.now() - datetime.fromisoformat(updated)
    return diff.total_seconds() < CACHE_TTL_HOURS * 3600


# ─────────────────────────────────────────────────────
# 종목 분류
# ─────────────────────────────────────────────────────
def classify_stock(ticker: str, name: str, market: str, market_cap: float) -> dict:
    is_us = market in ("NASDAQ", "NYSE", "AMEX", "NAS", "NYS")

    if is_us:
        if ticker in LEVERAGED_ETFS:
            return {"size": "레버리지ETF", "asset_type": "ETF", "market_region": "미국"}
        if ticker in BROAD_INDEX_ETFS:
            return {"size": "지수ETF", "asset_type": "ETF", "market_region": "미국"}
        if "ETF" in name.upper():
            return {"size": "섹터ETF", "asset_type": "ETF", "market_region": "미국"}
        cap_usd = market_cap / 1350
        if cap_usd >= 200_000_000_000:
            size = "MegaCap"
        elif cap_usd >= 10_000_000_000:
            size = "LargeCap"
        elif cap_usd >= 2_000_000_000:
            size = "MidCap"
        else:
            size = "SmallCap"
        return {"size": size, "asset_type": "주식", "market_region": "미국"}

    if any(name.startswith(p) for p in KR_ETF_PREFIXES) or "ETF" in name:
        if "레버리지" in name or "2X" in name:
            return {"size": "레버리지ETF", "asset_type": "ETF", "market_region": "한국"}
        if "인버스" in name:
            return {"size": "인버스ETF", "asset_type": "ETF", "market_region": "한국"}
        return {"size": "지수ETF", "asset_type": "ETF", "market_region": "한국"}

    if market in ("KOSDAQ", "KSQ"):
        return {"size": "코스닥", "asset_type": "주식", "market_region": "한국"}

    if market_cap >= 5_000_000_000_000:
        size = "대형주"
    elif market_cap >= 1_000_000_000_000:
        size = "중형주"
    else:
        size = "소형주"
    return {"size": size, "asset_type": "주식", "market_region": "한국"}


# ─────────────────────────────────────────────────────
# ATR 계산
# ─────────────────────────────────────────────────────
def calc_atr_from_candles(candles: list[dict], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(len(candles) - 1):
        curr, prev = candles[i], candles[i + 1]
        high, low, prev_close = float(curr["high"]), float(curr["low"]), float(prev["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
        if len(true_ranges) >= period:
            break
    if not true_ranges:
        return None
    atr = sum(true_ranges) / len(true_ranges)
    current_price = float(candles[0]["close"])
    return round(atr / current_price * 100, 2) if current_price > 0 else None


# ─────────────────────────────────────────────────────
# ATR 기반 트레일링 % 계산 (AI 대체)
# ─────────────────────────────────────────────────────
def calc_trailing_by_atr(classification: dict, atr_pct: Optional[float]) -> dict:
    size = classification.get("size", "중형주")
    fallback_drop, fallback_trigger = FALLBACK_TRAILING.get(size, (10.0, 5.0))

    if atr_pct and atr_pct > 0:
        multiplier = ATR_MULTIPLIER.get(size, 2.5)
        raw_drop = round(atr_pct * multiplier, 1)

        # 상한/하한 클램프 적용
        min_drop, max_drop = ATR_CLAMP.get(size, (5.0, 20.0))
        drop = max(min_drop, min(max_drop, raw_drop))
        trigger = round(drop * 0.6, 1)  # 트리거는 트레일링 폭의 60%

        reason = (
            f"ATR {atr_pct}% × {multiplier}배 = {raw_drop}% "
            f"→ {size} 범위({min_drop}~{max_drop}%) 적용 → {drop}%"
        )
        confidence = "high" if len([1]) else "medium"  # ATR 있으면 high
    else:
        # ATR 데이터 없으면 분류 기반 기본값
        drop = fallback_drop
        trigger = fallback_trigger
        reason = f"{size} 분류 기본값 적용 (ATR 데이터 없음)"
        confidence = "low"

    return {
        "trailing_drop_pct": drop,
        "trailing_trigger_pct": trigger,
        "reason": reason,
        "confidence": confidence,
    }


# ─────────────────────────────────────────────────────
# 메인 진입점
# ─────────────────────────────────────────────────────
def get_trailing_config(holding: dict, candles: list[dict] = None) -> dict:
    ticker = holding["ticker"]
    cache = _load_cache()

    if ticker in cache and _is_cache_fresh(cache[ticker]):
        logger.info(f"[트레일링] {ticker} 캐시 사용")
        return cache[ticker]

    atr_pct = calc_atr_from_candles(candles) if candles else None
    classification = classify_stock(
        ticker=ticker,
        name=holding.get("name", ""),
        market=holding.get("market", "KOSPI"),
        market_cap=holding.get("market_cap", 0),
    )

    result_core = calc_trailing_by_atr(classification, atr_pct)

    result = {
        **result_core,
        "ticker": ticker,
        "classification": classification,
        "atr_pct": atr_pct,
        "source": "atr_auto",
        "updated_at": datetime.now().isoformat(),
    }

    logger.info(
        f"[트레일링] {ticker} ({classification['size']}) "
        f"ATR={atr_pct}% → 트레일링 {result['trailing_drop_pct']}% "
        f"({result['confidence']})"
    )

    cache[ticker] = result
    _save_cache(cache)
    return result


def refresh_all_trailing_configs(holdings: list[dict], candles_map: dict = None) -> dict:
    result = {}
    for h in holdings:
        ticker = h["ticker"]
        candles = (candles_map or {}).get(ticker, [])
        try:
            result[ticker] = get_trailing_config(h, candles)
        except Exception as e:
            logger.error(f"[트레일링] {ticker} 실패: {e}")
    return result
