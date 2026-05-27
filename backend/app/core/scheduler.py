"""
주기적 매도 검사 스케줄러
- 매 5분       : 현재가 체크 → 매도 조건 판단 → 텔레그램 알림
- 매시간 정각  : 장중 수익률 리포트 (한국장 09~15시 / 미국장 23~06시)
- 월요일 08:50 : AI 트레일링 비율 갱신 → 텔레그램 알림
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from app.core.sell_engine import evaluate_sell, execute_sell, peak_tracker, calc_portfolio_summary
from app.services.kis_client import get_kis_client
from app.services.trailing_advisor import get_trailing_config, _load_cache
from app.services.telegram import get_notifier
from app.core.state import get_settings, append_sell_log

logger = logging.getLogger(__name__)

SEOUL_TZ = ZoneInfo("Asia/Seoul")
scheduler = BackgroundScheduler(timezone=SEOUL_TZ)


def _now_seoul() -> datetime:
    return datetime.now(SEOUL_TZ)


def is_market_open(market: str = "KR") -> bool:
    now = _now_seoul()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    if market == "US":
        return t >= 2330 or t <= 600
    return 900 <= t <= 1530


# ─────────────────────────────────────────────────
# 공통: 보유종목 + 요약 조회
# ─────────────────────────────────────────────────
def _build_report_data() -> tuple[dict, list[dict]]:
    client = get_kis_client()
    settings = get_settings()
    seed_money = settings.get("seed_money", 1_000_000)
    cache = _load_cache()

    holdings_raw = client.get_holdings()
    out2 = client.get_last_domestic_balance_output2()
    summary = calc_portfolio_summary(holdings_raw, seed_money, [], out2)

    holdings_enriched = []
    for h in holdings_raw:
        trailing_config = cache.get(h["ticker"])
        client.enrich_holding_trading_meta(h, trailing_config)
        peak_info = peak_tracker.update(h["ticker"], h["current_price"], h["avg_price"])
        signal = evaluate_sell(h, settings, trailing_config)
        holdings_enriched.append({
            **h,
            "peak_info": peak_info,
            "trailing_config": trailing_config or {},
            "sell_signal": signal,
            "should_sell": signal is not None,
        })

    return summary, holdings_enriched


# ─────────────────────────────────────────────────
# 매 5분: 매도 조건 체크
# ─────────────────────────────────────────────────
def run_sell_check():
    settings = get_settings()
    if not settings.get("enabled", True):
        return

    client = get_kis_client()
    notifier = get_notifier()

    try:
        holdings = client.get_holdings()
    except Exception as e:
        logger.error(f"잔고조회 실패: {e}")
        notifier.notify_system(f"잔고 조회 실패\n{e}", level="error")
        return

    cache = _load_cache()
    logger.info(f"[스케줄] 보유종목 {len(holdings)}개 검사")

    for holding in holdings:
        ticker = holding["ticker"]
        trailing_config = cache.get(ticker)
        client.enrich_holding_trading_meta(holding, trailing_config)

        market = holding.get("market", "KOSPI")
        is_us = bool(holding.get("ovrs_excg_cd")) or market in (
            "NASDAQ", "NYSE", "AMEX", "NAS", "NYS", "AMS", "NASD"
        )

        if is_us and not is_market_open("US"):
            continue
        if not is_us and not is_market_open("KR"):
            continue

        signal = evaluate_sell(holding, settings, trailing_config)

        if signal:
            logger.warning(f"[매도신호] {ticker} — {signal['reason_label']}")
            notifier.notify_sell_signal(signal)

            result = execute_sell(signal)
            result["checked_at"] = _now_seoul().isoformat()
            append_sell_log(result)

            notifier.notify_sell_result(result)


# ─────────────────────────────────────────────────
# 매시간 정각: 수익률 리포트
# ─────────────────────────────────────────────────
def send_hourly_report(market: str = "KR"):
    """장중 매시간 정각에 수익률 현황 발송"""
    notifier = get_notifier()
    try:
        summary, holdings = _build_report_data()
        # 리포트 타입은 시간대로 구분
        report_type = "open" if _now_seoul().hour < 12 else "close"
        notifier.notify_daily_report(summary, holdings, report_type=report_type)
        logger.info(f"[텔레그램] 시간별 리포트 발송 ({market} 장)")
    except Exception as e:
        logger.error(f"리포트 발송 실패: {e}")
        notifier.notify_system(f"리포트 조회 실패\n{e}", level="error")


# 하위 호환용 — API 라우터에서 직접 호출
def send_daily_report(report_type: str = "close"):
    notifier = get_notifier()
    try:
        summary, holdings = _build_report_data()
        notifier.notify_daily_report(summary, holdings, report_type=report_type)
        logger.info(f"[텔레그램] 리포트 발송 완료 ({report_type})")
    except Exception as e:
        notifier.notify_system(f"리포트 조회 실패\n{e}", level="error")


# ─────────────────────────────────────────────────
# 월요일 08:50: AI 트레일링 갱신
# ─────────────────────────────────────────────────
def refresh_trailing_configs_job():
    logger.info("[스케줄] AI 트레일링 설정 갱신 시작")
    client = get_kis_client()
    notifier = get_notifier()
    updated_configs = {}

    try:
        holdings = client.get_holdings()
        for h in holdings:
            try:
                client.enrich_holding_trading_meta(h)
                if not h.get("market_cap"):
                    mkt = client.get_stock_market_info(h["ticker"])
                    h.setdefault("market", mkt.get("market", "KOSPI"))
                    h["market_cap"] = mkt.get("market_cap", 0)
                candles = client.get_daily_candles(h["ticker"], days=20, market=h.get("market", "KOSPI"))
                config = get_trailing_config(h, candles)
                updated_configs[h["ticker"]] = config
                logger.info(
                    f"  {h['ticker']} → 트레일링 {config.get('trailing_drop_pct')}% "
                    f"[ATR {config.get('atr_pct','?')}%] 신뢰도: {config.get('confidence','?')}"
                )
            except Exception as e:
                logger.error(f"  {h['ticker']} 갱신 실패: {e}")
    except Exception as e:
        logger.error(f"잔고조회 실패: {e}")
        notifier.notify_system(f"트레일링 갱신 실패\n{e}", level="error")
        return

    if updated_configs:
        notifier.notify_trailing_update(updated_configs)


# ─────────────────────────────────────────────────
# 스케줄러 시작
# ─────────────────────────────────────────────────
def start_scheduler():
    # 매 5분: 매도 체크
    scheduler.add_job(
        run_sell_check,
        trigger=IntervalTrigger(minutes=5),
        id="sell_check",
        replace_existing=True,
    )

    # 한국장 매시간 정각 (09:00 ~ 15:00 KST, 평일)
    scheduler.add_job(
        lambda: send_hourly_report("KR"),
        trigger=CronTrigger(
            hour="9-15", minute=0, day_of_week="mon-fri", timezone=SEOUL_TZ
        ),
        id="report_hourly_kr",
        replace_existing=True,
    )

    # 미국장 매시간 정각 (23:00, 00:00 ~ 06:00 KST, 평일)
    scheduler.add_job(
        lambda: send_hourly_report("US"),
        trigger=CronTrigger(
            hour="23,0,1,2,3,4,5,6", minute=0, day_of_week="mon-fri", timezone=SEOUL_TZ
        ),
        id="report_hourly_us",
        replace_existing=True,
    )

    # 월요일 08:50 KST: AI 트레일링 갱신
    scheduler.add_job(
        refresh_trailing_configs_job,
        trigger=CronTrigger(hour=8, minute=50, day_of_week="mon", timezone=SEOUL_TZ),
        id="refresh_trailing",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "스케줄러 시작 (타임존 Asia/Seoul) — "
        "한국장 리포트 09~15시 정각, 미국장 23·0~6시 정각, 트레일링 월 08:50"
    )
    get_notifier().notify_system("PeakExit 시스템 시작\n매도 모니터링을 시작합니다.", level="info")


def stop_scheduler():
    scheduler.shutdown()
    get_notifier().notify_system("PeakExit 시스템 종료", level="warning")
