from fastapi import APIRouter, HTTPException, BackgroundTasks
from app.services.kis_client import get_kis_client
from app.services.trailing_advisor import get_trailing_config, refresh_all_trailing_configs, _load_cache
from app.core.sell_engine import evaluate_sell, peak_tracker, calc_portfolio_summary
from app.core.state import get_settings

router = APIRouter()


def _enrich_holding(h: dict, settings: dict) -> dict:
    """보유종목에 시장정보, ATR, AI 트레일링 설정, 매도신호 추가"""
    client = get_kis_client()
    ticker = h["ticker"]

    # 시장 정보 (캐시된 trailing_config에서 재사용 가능)
    cache = _load_cache()
    trailing_config = cache.get(ticker)

    client.enrich_holding_trading_meta(h, trailing_config)

    # 목록 화면: 일봉/시세 추가 호출 없이 캐시 또는 분류 기본값만 (타임아웃·누락 방지)
    if not trailing_config:
        if h.get("currency") == "USD":
            h.setdefault("market", h.get("market") or "NASDAQ")
            h.setdefault("market_cap", 0)
        elif not h.get("market"):
            try:
                mkt_info = client.get_stock_market_info(ticker)
                h["market"] = mkt_info.get("market", "KOSPI")
                h["market_cap"] = mkt_info.get("market_cap", 0)
            except Exception:
                h.setdefault("market", "KOSPI")
                h.setdefault("market_cap", 0)
        trailing_config = get_trailing_config(h, candles=None)
    else:
        region = trailing_config.get("classification", {}).get("market_region", "")
        if region == "미국":
            h.setdefault("market", "NASDAQ")
        elif region == "한국":
            h.setdefault("market", "KOSPI")
        h.setdefault("market_cap", 0)

    peak_info = peak_tracker.update(h["ticker"], h["current_price"], h["avg_price"])
    signal = evaluate_sell(h, settings, trailing_config)

    return {
        **h,
        "peak_info": peak_info,
        "trailing_config": {
            "trailing_drop_pct": trailing_config.get("trailing_drop_pct"),
            "trailing_trigger_pct": trailing_config.get("trailing_trigger_pct"),
            "reason": trailing_config.get("reason"),
            "confidence": trailing_config.get("confidence"),
            "atr_pct": trailing_config.get("atr_pct"),
            "classification": trailing_config.get("classification", {}),
            "source": trailing_config.get("source", "unknown"),
            "updated_at": trailing_config.get("updated_at"),
        },
        "sell_signal": signal,
        "should_sell": signal is not None,
    }


@router.get("/dashboard")
def get_dashboard():
    """잔고 1회 스냅샷으로 요약 + 보유(분석) 동시 반환 — 요약 종목 수와 테이블 행 수 불일치 방지."""
    client = get_kis_client()
    settings = get_settings()
    seed_money = settings.get("seed_money", 1_000_000)
    try:
        holdings = client.get_holdings()
        out2 = client.get_last_domestic_balance_output2()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터 조회 실패: {e}")

    summary = calc_portfolio_summary(holdings, seed_money, [], out2)
    summary["holdings_count"] = len(holdings)

    result = []
    for h in holdings:
        try:
            result.append(_enrich_holding(h, settings))
        except Exception as e:
            result.append({
                **h,
                "quantity": int(h.get("quantity") or 0),
                "avg_price": float(h.get("avg_price") or 0),
                "current_price": float(h.get("current_price") or 0),
                "profit_rate": float(h.get("profit_rate") or 0),
                "error": str(e),
                "should_sell": False,
                "peak_info": {},
                "trailing_config": {},
                "sell_signal": None,
            })
    return {"holdings": result, "summary": summary}


@router.get("/holdings")
def get_holdings():
    """보유 종목 + AI 트레일링 분석 + 매도 신호"""
    client = get_kis_client()
    settings = get_settings()
    try:
        holdings = client.get_holdings()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"잔고 조회 실패: {e}")

    result = []
    for h in holdings:
        try:
            result.append(_enrich_holding(h, settings))
        except Exception as e:
            base = {
                **h,
                "quantity": int(h.get("quantity") or 0),
                "avg_price": float(h.get("avg_price") or 0),
                "current_price": float(h.get("current_price") or 0),
                "profit_rate": float(h.get("profit_rate") or 0),
                "error": str(e),
                "should_sell": False,
                "peak_info": {},
                "trailing_config": {},
                "sell_signal": None,
            }
            result.append(base)
    return result


@router.post("/refresh-trailing")
def refresh_trailing(background_tasks: BackgroundTasks):
    """전체 보유종목 AI 트레일링 설정 강제 갱신 (백그라운드)"""
    def _refresh():
        client = get_kis_client()
        try:
            holdings = client.get_holdings()
            candles_map = {}
            for h in holdings:
                client.enrich_holding_trading_meta(h)
                if not h.get("market_cap"):
                    mkt = client.get_stock_market_info(h["ticker"])
                    h.setdefault("market", mkt.get("market", "KOSPI"))
                    h["market_cap"] = mkt.get("market_cap", 0)
                candles_map[h["ticker"]] = client.get_daily_candles(
                    h["ticker"], days=20, market=h.get("market", "KOSPI")
                )
            refresh_all_trailing_configs(holdings, candles_map)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"트레일링 갱신 실패: {e}")

    background_tasks.add_task(_refresh)
    return {"message": "AI 트레일링 분석 백그라운드 실행 중..."}


@router.get("/trailing-configs")
def get_trailing_configs():
    """현재 캐싱된 종목별 트레일링 설정 조회"""
    return _load_cache()


@router.get("/portfolio-summary")
def get_portfolio_summary():
    client = get_kis_client()
    settings = get_settings()
    seed_money = settings.get("seed_money", 1_000_000)
    try:
        holdings = client.get_holdings()
        out2 = client.get_last_domestic_balance_output2()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터 조회 실패: {e}")

    summary = calc_portfolio_summary(holdings, seed_money, [], out2)
    return summary


@router.post("/manual-sell/{ticker}")
def manual_sell(ticker: str):
    from app.core.sell_engine import execute_sell
    from app.core.state import append_sell_log
    from datetime import datetime
    client = get_kis_client()
    try:
        holdings = client.get_holdings()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    holding = next((h for h in holdings if h["ticker"] == ticker), None)
    if not holding:
        raise HTTPException(status_code=404, detail=f"{ticker} 보유 종목 없음")

    from app.services.trailing_advisor import _load_cache

    cache = _load_cache()
    client.enrich_holding_trading_meta(holding, cache.get(ticker))

    signal = {
        "ticker": ticker,
        "name": holding["name"],
        "quantity": holding["quantity"],
        "reason": "MANUAL",
        "reason_label": "수동 매도",
        "profit_rate": holding["profit_rate"],
        "current_price": holding["current_price"],
    }
    if holding.get("currency"):
        signal["currency"] = holding["currency"]
    if holding.get("ovrs_excg_cd"):
        signal["ovrs_excg_cd"] = holding["ovrs_excg_cd"]
    if holding.get("ovrs_quote_excd"):
        signal["ovrs_quote_excd"] = holding["ovrs_quote_excd"]
    result = execute_sell(signal)
    result["checked_at"] = datetime.now().isoformat()
    append_sell_log(result)
    return result


@router.post("/run-check")
def run_check_now():
    from app.core.scheduler import run_sell_check
    run_sell_check()
    return {"message": "매도 검사 완료"}
