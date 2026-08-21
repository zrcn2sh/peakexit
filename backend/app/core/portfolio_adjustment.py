"""
총자산 — 미반영 현금 보정.

  미반영 현금 = 당일 매도 체결 합(원화) − 당일 매수 체결 합(원화) − dnca_tot_amt
  (양수일 때만 총자산에 가산)

종목별 매도·잔고 유무와 무관하게 당일 순현금 흐름으로 계산해,
매도 후 다른 종목 매수 시 이중 계산을 막는다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SEOUL_TZ = ZoneInfo("Asia/Seoul")
NY_TZ = ZoneInfo("America/New_York")

# 미국 매도 → 원화/USD 예수금 잔고 반영 지연(최대 약 1~2주). 잔고에 없는 종목 SELL 보정 기간.
US_SELL_LOOKBACK_CALENDAR_DAYS = 10
US_SELL_LOG_LOOKBACK_CALENDAR_DAYS = 14
US_WEEKEND_ET_SESSIONS = 7


def parse_ccld_datetime_kst(ord_dt: str, time_str: str = "") -> Optional[datetime]:
    """체결/주문 일시를 KST aware datetime으로 변환."""
    d = str(ord_dt or "").strip()
    if len(d) < 8:
        return None
    d = d[:8]
    t = str(time_str or "000000").strip().replace(":", "")
    if not t:
        t = "000000"
    t = t.zfill(6)[:6]
    try:
        return datetime.strptime(f"{d}{t}", "%Y%m%d%H%M%S").replace(tzinfo=SEOUL_TZ)
    except ValueError:
        try:
            return datetime.strptime(d, "%Y%m%d").replace(tzinfo=SEOUL_TZ)
        except ValueError:
            return None


def trade_calendar_date(trade: dict, now_kst: Optional[datetime] = None) -> Optional[str]:
    """
    '당일' 판정용 거래일 (YYYY-MM-DD).
    - 한국: KST 날짜
    - 미국: America/New_York 날짜 (DST 자동)
    """
    ccld = trade.get("ccld_at_kst")
    if not isinstance(ccld, datetime):
        return None
    if ccld.tzinfo is None:
        ccld = ccld.replace(tzinfo=SEOUL_TZ)
    region = trade.get("region", "KR")
    if region == "US":
        return ccld.astimezone(NY_TZ).date().isoformat()
    return ccld.astimezone(SEOUL_TZ).date().isoformat()


def is_trade_on_business_today(trade: dict, now_kst: Optional[datetime] = None) -> bool:
    """종목별 거래일 기준으로 '오늘' 체결인지."""
    now = now_kst or datetime.now(SEOUL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SEOUL_TZ)
    t_date = trade_calendar_date(trade, now)
    if not t_date:
        return False
    region = trade.get("region", "KR")
    if region == "US":
        today = now.astimezone(NY_TZ).date().isoformat()
    else:
        today = now.astimezone(SEOUL_TZ).date().isoformat()
    return t_date == today


def needs_us_weekend_settlement_window(now_kst: datetime) -> bool:
    """
    KST 토·일: 미국 매도 대금이 월요일 08:40~08:45 전까지 잔고에 반영되지 않음.
    월요일 08:45 이전도 동일하게 보정 구간으로 본다.
    """
    if now_kst.tzinfo is None:
        now_kst = now_kst.replace(tzinfo=SEOUL_TZ)
    wd = now_kst.weekday()  # 월=0 … 토=5, 일=6
    if wd >= 5:
        return True
    if wd == 0 and (now_kst.hour * 100 + now_kst.minute) < 845:
        return True
    return False


def recent_us_et_trade_dates(now_kst: datetime, max_sessions: int = US_WEEKEND_ET_SESSIONS) -> set[str]:
    """보정 구간에 포함할 최근 미국(ET) 영업일."""
    if now_kst.tzinfo is None:
        now_kst = now_kst.replace(tzinfo=SEOUL_TZ)
    et_today = now_kst.astimezone(NY_TZ).date()
    found: list[str] = []
    for i in range(10):
        d = et_today - timedelta(days=i)
        if d.weekday() < 5:
            found.append(d.isoformat())
        if len(found) >= max_sessions:
            break
    return set(found)


def is_us_trade_in_settlement_window(
    trade: dict,
    now_kst: datetime,
    *,
    max_sessions: int = US_WEEKEND_ET_SESSIONS,
) -> bool:
    """KST 주말·월 오전: ET 최근 영업일 체결 매도를 보정 후보로 본다."""
    t_date = trade_calendar_date(trade, now_kst)
    if not t_date:
        return False
    return t_date in recent_us_et_trade_dates(now_kst, max_sessions=max_sessions)


def trade_datetime_kst(trade: dict) -> Optional[datetime]:
    """체결 시각(KST). API date(YYYYMMDD)만 있어도 반환."""
    ccld = trade.get("ccld_at_kst")
    if isinstance(ccld, datetime):
        if ccld.tzinfo is None:
            return ccld.replace(tzinfo=SEOUL_TZ)
        return ccld.astimezone(SEOUL_TZ)
    d = str(trade.get("date") or "").strip()
    if len(d) >= 8:
        return parse_ccld_datetime_kst(d[:8], "")
    return None


def trade_within_calendar_days(
    trade: dict,
    now_kst: datetime,
    max_days: int,
) -> bool:
    dt = trade_datetime_kst(trade)
    if not dt:
        return False
    if now_kst.tzinfo is None:
        now_kst = now_kst.replace(tzinfo=SEOUL_TZ)
    age_sec = (now_kst - dt).total_seconds()
    return 0 <= age_sec <= max(1, max_days) * 86400


def is_us_sell_eligible_for_adjustment(trade: dict, now_kst: Optional[datetime] = None) -> bool:
    """
    해외(미국) SELL — 잔고에 종목이 없을 때 매도 대금 보정 후보.
    당일·주말 구간 ET 영업일 + 최근 N일(정산 지연) 체결 포함.
    """
    now = now_kst or datetime.now(SEOUL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SEOUL_TZ)

    if trade.get("source") == "sell_log":
        if trade_within_calendar_days(trade, now, US_SELL_LOG_LOOKBACK_CALENDAR_DAYS):
            return True
        return is_trade_on_business_today(trade, now)

    if needs_us_weekend_settlement_window(now):
        if is_us_trade_in_settlement_window(trade, now):
            return True

    if is_trade_on_business_today(trade, now):
        return True

    return trade_within_calendar_days(trade, now, US_SELL_LOOKBACK_CALENDAR_DAYS)


def is_trade_eligible_for_adjustment(trade: dict, now_kst: Optional[datetime] = None) -> bool:
    """하위 호환 — 현금 보정 구간 판정."""
    return is_trade_in_cash_adjustment_window(trade, now_kst)


def is_trade_in_cash_adjustment_window(
    trade: dict,
    now_kst: Optional[datetime] = None,
) -> bool:
    """
    현금 보정에 포함할 체결(매도·매수)인지.
    - 국내: KST 당일
    - 미국: ET 당일 (KST 토·일·월 08:45 전엔 최근 ET 영업일 포함)
    """
    now = now_kst or datetime.now(SEOUL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SEOUL_TZ)
    region = trade.get("region", "KR")
    if region == "US":
        if needs_us_weekend_settlement_window(now):
            return is_us_trade_in_settlement_window(trade, now) or is_trade_on_business_today(
                trade, now
            )
        return is_trade_on_business_today(trade, now)
    return is_trade_on_business_today(trade, now)


def trade_amount_krw(trade: dict, usd_krw_rate: Optional[float]) -> float:
    """체결 금액 → 원화."""
    amt = float(trade.get("amount") or 0)
    if amt <= 0:
        return 0.0
    if trade.get("currency") == "USD":
        fx = float(usd_krw_rate or 0)
        if fx <= 0:
            return 0.0
        return round(amt * fx)
    return round(amt)


def _kis_float_o2(output2: Optional[dict], *keys: str) -> float:
    from app.core.account_valuation import kis_float

    o = output2 if isinstance(output2, dict) else {}
    return kis_float(o, *keys)


def total_liquid_cash_krw(
    domestic_output2: Optional[dict],
    overseas=None,
    usd_krw_rate: Optional[float] = None,
) -> tuple[float, float, float]:
    """
    인식 가능 현금(원화) = 국내 dnca_tot_amt + 외화예수금(원화).
    Returns: (합계, 국내예수금, 외화예수금원화)
    """
    from app.core.account_valuation import overseas_cash_from_valuation

    dom_cash = _kis_float_o2(domestic_output2, "dnca_tot_amt")
    ov_usd, ov_krw = overseas_cash_from_valuation(overseas, float(usd_krw_rate or 0))

    if ov_krw <= 0:
        frcr = _kis_float_o2(
            domestic_output2,
            "frcr_dncl_amt_2",
            "frcr_dnca_tot_amt",
            "ovrs_frcr_dncl_amt",
        )
        rate = _kis_float_o2(domestic_output2, "bass_exrt", "frst_bltn_exrt", "exrt") or float(
            usd_krw_rate or 0
        )
        if frcr > 0 and rate > 0:
            ov_krw = frcr * rate
            ov_usd = frcr

    total = dom_cash + ov_krw
    return round(total), round(dom_cash), round(ov_krw)


def _today_trade_flows_krw(
    trades: list[dict],
    domestic_output2: Optional[dict],
    usd_krw_rate: Optional[float],
    now_kst: datetime,
) -> tuple[float, float, str, list, list]:
    """당일 매도·매수 합(원화). output2 thdt_* 우선, 없으면 체결내역 합산."""
    now = now_kst
    if now.tzinfo is None:
        now = now.replace(tzinfo=SEOUL_TZ)

    trade_sell = 0.0
    trade_buy = 0.0
    sell_rows: list[dict] = []
    buy_rows: list[dict] = []

    for tr in trades or []:
        if not is_trade_in_cash_adjustment_window(tr, now):
            continue
        amt_krw = trade_amount_krw(tr, usd_krw_rate)
        if amt_krw <= 0:
            continue
        if tr.get("type") == "SELL":
            trade_sell += amt_krw
            sell_rows.append(tr)
        elif tr.get("type") == "BUY":
            trade_buy += amt_krw
            buy_rows.append(tr)

    api_sell = _kis_float_o2(domestic_output2, "thdt_sll_amt")
    api_buy = _kis_float_o2(domestic_output2, "thdt_buy_amt")

    us_sell = 0.0
    us_buy = 0.0
    kr_sell = 0.0
    kr_buy = 0.0
    for tr in sell_rows:
        if tr.get("region") == "US":
            us_sell += trade_amount_krw(tr, usd_krw_rate)
        else:
            kr_sell += trade_amount_krw(tr, usd_krw_rate)
    for tr in buy_rows:
        if tr.get("region") == "US":
            us_buy += trade_amount_krw(tr, usd_krw_rate)
        else:
            kr_buy += trade_amount_krw(tr, usd_krw_rate)

    if api_sell > 0 or api_buy > 0:
        today_sell = api_sell + us_sell
        today_buy = api_buy + us_buy
        source = "output2_thdt+us_trades"
    else:
        today_sell = trade_sell
        today_buy = trade_buy
        source = "trades"

    if today_sell <= 0 and trade_sell > 0:
        today_sell = trade_sell
        source = "trades_fallback"
    if today_buy <= 0 and trade_buy > 0:
        today_buy = trade_buy

    return today_sell, today_buy, source, sell_rows, buy_rows


def calc_unreflected_cash_adjustment_krw(
    trades: list[dict],
    domestic_output2: Optional[dict],
    usd_krw_rate: Optional[float],
    now_kst: Optional[datetime] = None,
    *,
    overseas=None,
) -> tuple[float, dict]:
    """
    미반영 현금 = 당일 매도 − 당일 매수 − (국내예수금 + 외화예수금원화).
    당일 매도/매수: output2 thdt_sll_amt·thdt_buy_amt 우선, 없으면 체결 합산.
    """
    now = now_kst or datetime.now(SEOUL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SEOUL_TZ)

    today_sell, today_buy, flow_src, sell_rows, buy_rows = _today_trade_flows_krw(
        trades, domestic_output2, usd_krw_rate, now
    )

    total_cash, dom_cash, ov_cash_krw = total_liquid_cash_krw(
        domestic_output2, overseas, usd_krw_rate
    )
    unreflected = today_sell - today_buy - total_cash
    adjustment = round(unreflected) if unreflected > 0 else 0.0

    mode = "us_weekend_window" if needs_us_weekend_settlement_window(now) else "today"
    detail = {
        "method": "cash_net_minus_liquid",
        "flow_source": flow_src,
        "today_sell_krw": round(today_sell),
        "today_buy_krw": round(today_buy),
        "domestic_cash_krw": round(dom_cash),
        "overseas_cash_krw": round(ov_cash_krw),
        "current_cash_krw": round(total_cash),
        "unreflected_cash_krw": round(unreflected),
        "adjustment_krw": round(adjustment),
        "sell_trade_count": len(sell_rows),
        "buy_trade_count": len(buy_rows),
        "settlement_mode": mode,
        "api_thdt_sll": round(_kis_float_o2(domestic_output2, "thdt_sll_amt")),
        "api_thdt_buy": round(_kis_float_o2(domestic_output2, "thdt_buy_amt")),
    }

    if adjustment > 0:
        logger.info(
            "미반영 현금 보정 +%s원 (매도 %s − 매수 %s − 현금 %s[국내 %s+외화 %s] = %s) %s",
            f"{adjustment:,.0f}",
            f"{today_sell:,.0f}",
            f"{today_buy:,.0f}",
            f"{total_cash:,.0f}",
            f"{dom_cash:,.0f}",
            f"{ov_cash_krw:,.0f}",
            f"{unreflected:,.0f}",
            flow_src,
        )
    elif today_sell > 0 or today_buy > 0:
        logger.debug(
            "미반영 현금 보정 없음 (매도 %s − 매수 %s − 현금 %s = %s)",
            f"{today_sell:,.0f}",
            f"{today_buy:,.0f}",
            f"{total_cash:,.0f}",
            f"{unreflected:,.0f}",
        )

    return adjustment, detail


def resolve_account_base_krw(
    domestic_output2: dict,
    overseas_eval_krw: float = 0.0,
    holdings: Optional[list[dict]] = None,
    *,
    valuation_snapshot: Optional[object] = None,
) -> dict:
    """
    총자산(보정 전) = 국내 TTTC8434R output2.tot_evlu_amt + 해외 CTRP6504R(또는 TTTS3012R NASD) 원화.
    valuation_snapshot(AccountValuationSnapshot)이 있으면 API 문서 기준 파서 결과를 우선한다.
    """
    from app.core.account_valuation import (
        AccountValuationSnapshot,
        OverseasValuation,
        resolve_account_base_from_snapshot,
    )

    if valuation_snapshot is not None and isinstance(valuation_snapshot, AccountValuationSnapshot):
        from app.services.fx_rate import get_usd_krw_rate

        fx = get_usd_krw_rate()
        info = resolve_account_base_from_snapshot(
            valuation_snapshot,
            holdings=holdings or [],
            fx_rate=fx,
        )
        holdings = holdings or []
        if info["stocks_eval_domestic_krw"] <= 0:
            kr_eval = sum(h["eval_amount"] for h in holdings if h.get("currency", "KRW") != "USD")
            if kr_eval > 0:
                info["stocks_eval_domestic_krw"] = kr_eval
                info["stocks_eval_total_krw"] = kr_eval + float(info["stocks_eval_overseas_krw"])
        return info

    from app.services.fx_rate import get_usd_krw_rate

    fx = get_usd_krw_rate()
    ovrs = OverseasValuation(total_krw=max(0.0, float(overseas_eval_krw or 0)), source="legacy_float")
    snap = AccountValuationSnapshot.build(domestic_output2, ovrs)
    return resolve_account_base_from_snapshot(snap, holdings=holdings or [], fx_rate=fx)


def pending_settlement_mode(
    now_kst: Optional[datetime] = None,
    adjustments: Optional[list[dict]] = None,
) -> str:
    """API/프론트 표시용 보정 모드."""
    now = now_kst or datetime.now(SEOUL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SEOUL_TZ)
    for a in adjustments or []:
        if isinstance(a, dict) and a.get("settlement_mode"):
            return str(a["settlement_mode"])
    if needs_us_weekend_settlement_window(now):
        return "us_weekend_window"
    return "today"


def _holding_ticker_keys(holdings: list[dict]) -> set[str]:
    keys: set[str] = set()
    for h in holdings:
        t = (h.get("ticker") or "").strip().upper()
        if not t:
            continue
        keys.add(t)
        if h.get("currency") == "USD" or h.get("ovrs_excg_cd"):
            ocd = (h.get("ovrs_excg_cd") or "NASD").strip().upper()
            keys.add(f"{t}:{ocd}")
    return keys


def _trade_ticker_keys(trade: dict) -> set[str]:
    t = (trade.get("ticker") or "").strip().upper()
    if not t:
        return set()
    keys = {t}
    if trade.get("region") == "US":
        ocd = (trade.get("ovrs_excg_cd") or "NASD").strip().upper()
        keys.add(f"{t}:{ocd}")
    return keys


def trade_not_in_holdings(trade: dict, holdings: list[dict]) -> bool:
    """현재 잔고에 해당 종목이 없으면 True (전량 매도·미반영 추정)."""
    held = _holding_ticker_keys(holdings)
    keys = _trade_ticker_keys(trade)
    if not keys:
        return False
    return keys.isdisjoint(held)


def trades_from_sell_log(max_days: int = 14) -> list[dict]:
    """PeakExit 매도 이력 → 체결 보정용 trade 형식 (KIS 체결 API 실패 시 폴백)."""
    from app.core.state import get_sell_log

    now = datetime.now(SEOUL_TZ)
    cutoff = (now - timedelta(days=max_days)).date()
    out: list[dict] = []

    for entry in get_sell_log():
        if not entry.get("success"):
            continue
        checked_raw = entry.get("checked_at") or ""
        ccld_kst: Optional[datetime] = None
        if checked_raw:
            try:
                ccld_kst = datetime.fromisoformat(str(checked_raw).replace("Z", "+00:00"))
                if ccld_kst.tzinfo is None:
                    ccld_kst = ccld_kst.replace(tzinfo=SEOUL_TZ)
                else:
                    ccld_kst = ccld_kst.astimezone(SEOUL_TZ)
            except ValueError:
                pass
        if ccld_kst and ccld_kst.date() < cutoff:
            continue

        region = "US" if entry.get("ovrs_excg_cd") or entry.get("currency") == "USD" else "KR"
        qty = int(entry.get("quantity") or 0)
        if qty <= 0:
            continue
        amt = entry.get("sell_total")
        if amt is None:
            sell_p = float(entry.get("sell_price") or entry.get("current_price") or 0)
            amt = sell_p * qty
        amt = float(amt or 0)
        if amt <= 0:
            continue

        out.append({
            "region": region,
            "type": "SELL",
            "ticker": (entry.get("ticker") or "").strip(),
            "name": entry.get("name", ""),
            "quantity": qty,
            "amount": amt,
            "currency": entry.get("currency") or ("USD" if region == "US" else "KRW"),
            "ccld_at_kst": ccld_kst,
            "ovrs_excg_cd": entry.get("ovrs_excg_cd"),
            "source": "sell_log",
        })
    return out


def _add_business_days(start: date, days: int) -> date:
    """주말 제외 영업일 가산 (공휴일 미반영)."""
    cur = start
    added = 0
    while added < days:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


def _trade_exec_date(trade: dict, region: str) -> Optional[date]:
    ccld = trade.get("ccld_at_kst")
    if isinstance(ccld, datetime):
        if ccld.tzinfo is None:
            ccld = ccld.replace(tzinfo=SEOUL_TZ)
        if region == "US":
            return ccld.astimezone(NY_TZ).date()
        return ccld.astimezone(SEOUL_TZ).date()
    d = str(trade.get("date") or "").strip()[:8]
    if len(d) == 8:
        try:
            return datetime.strptime(d, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def trade_settlement_date(trade: dict) -> Optional[date]:
    """매도·매수 결제일. 국내 T+2, 해외(미국) T+1."""
    region = trade.get("region", "KR")
    exec_d = _trade_exec_date(trade, region)
    if not exec_d:
        return None
    lag = 1 if region == "US" else 2
    return _add_business_days(exec_d, lag)


def is_pending_sell_settlement(trade: dict, now_kst: Optional[datetime] = None) -> bool:
    """체결됐으나 결제일 전 매도(T+2/T+1 미결제)."""
    if (trade.get("type") or "").upper() != "SELL":
        return False
    settle = trade_settlement_date(trade)
    if not settle:
        return False
    now = now_kst or datetime.now(SEOUL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SEOUL_TZ)
    region = trade.get("region", "KR")
    today = now.astimezone(NY_TZ).date() if region == "US" else now.astimezone(SEOUL_TZ).date()
    return settle > today


def is_pending_buy_settlement(trade: dict, now_kst: Optional[datetime] = None) -> bool:
    """체결됐으나 결제일 전 매수(T+2/T+1 미결제). 미결제 매도 대금으로 매수한 경우 총자산 이중 가산 방지."""
    if (trade.get("type") or "").upper() != "BUY":
        return False
    settle = trade_settlement_date(trade)
    if not settle:
        return False
    now = now_kst or datetime.now(SEOUL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SEOUL_TZ)
    region = trade.get("region", "KR")
    today = now.astimezone(NY_TZ).date() if region == "US" else now.astimezone(SEOUL_TZ).date()
    return settle > today


def trade_settlement_amount_krw(trade: dict, usd_krw_rate: Optional[float]) -> float:
    """체결금액 → 원화 (해외는 체결 API 환율 우선)."""
    amt = float(trade.get("amount") or 0)
    if amt <= 0:
        return 0.0
    if trade.get("currency") == "USD" or trade.get("region") == "US":
        fx = float(trade.get("fx_rate") or usd_krw_rate or 0)
        if fx <= 0:
            return 0.0
        return round(amt * fx)
    return round(amt)


def calc_pending_sell_settlement_krw(
    trades: list[dict],
    usd_krw_rate: Optional[float],
    now_kst: Optional[datetime] = None,
) -> tuple[float, dict]:
    """
    매도 미결제 금액 (원화).
    - 국내: T+2 미결제 SELL
    - 해외: T+1 미결제 SELL
    """
    now = now_kst or datetime.now(SEOUL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SEOUL_TZ)

    dom_krw = 0.0
    ov_krw = 0.0
    dom_rows: list[dict] = []
    ov_rows: list[dict] = []

    for tr in trades or []:
        if not is_pending_sell_settlement(tr, now):
            continue
        amt_krw = trade_settlement_amount_krw(tr, usd_krw_rate)
        if amt_krw <= 0:
            continue
        settle = trade_settlement_date(tr)
        row = {
            "ticker": tr.get("ticker"),
            "name": tr.get("name"),
            "region": tr.get("region", "KR"),
            "amount_krw": amt_krw,
            "settlement_date": settle.isoformat() if settle else "",
            "trade_date": trade_calendar_date(tr) or "",
            "source": tr.get("source", ""),
        }
        if tr.get("region") == "US":
            ov_krw += amt_krw
            ov_rows.append(row)
        else:
            dom_krw += amt_krw
            dom_rows.append(row)

    total = round(dom_krw + ov_krw)
    detail = {
        "method": "pending_sell_settlement",
        "domestic_krw": round(dom_krw),
        "overseas_krw": round(ov_krw),
        "total_krw": total,
        "domestic_trades": dom_rows,
        "overseas_trades": ov_rows,
        "domestic_count": len(dom_rows),
        "overseas_count": len(ov_rows),
    }
    if total > 0:
        logger.info(
            "매도 미결제 +%s원 (국내 T+2 %s + 해외 T+1 %s)",
            f"{total:,.0f}",
            f"{dom_krw:,.0f}",
            f"{ov_krw:,.0f}",
        )
    return total, detail


def calc_pending_buy_settlement_krw(
    trades: list[dict],
    usd_krw_rate: Optional[float],
    now_kst: Optional[datetime] = None,
) -> tuple[float, dict]:
    """
    매수 미결제 금액 (원화).
    - 국내: T+2 미결제 BUY
    - 해외: T+1 미결제 BUY
    미결제 매도 대금으로 재매수한 금액 — 총자산에서 매도 미결제와 상계한다.
    """
    now = now_kst or datetime.now(SEOUL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SEOUL_TZ)

    dom_krw = 0.0
    ov_krw = 0.0
    dom_rows: list[dict] = []
    ov_rows: list[dict] = []

    for tr in trades or []:
        if not is_pending_buy_settlement(tr, now):
            continue
        amt_krw = trade_settlement_amount_krw(tr, usd_krw_rate)
        if amt_krw <= 0:
            continue
        settle = trade_settlement_date(tr)
        row = {
            "ticker": tr.get("ticker"),
            "name": tr.get("name"),
            "region": tr.get("region", "KR"),
            "amount_krw": amt_krw,
            "settlement_date": settle.isoformat() if settle else "",
            "trade_date": trade_calendar_date(tr) or "",
            "source": tr.get("source", ""),
        }
        if tr.get("region") == "US":
            ov_krw += amt_krw
            ov_rows.append(row)
        else:
            dom_krw += amt_krw
            dom_rows.append(row)

    total = round(dom_krw + ov_krw)
    detail = {
        "method": "pending_buy_settlement",
        "domestic_krw": round(dom_krw),
        "overseas_krw": round(ov_krw),
        "total_krw": total,
        "domestic_trades": dom_rows,
        "overseas_trades": ov_rows,
        "domestic_count": len(dom_rows),
        "overseas_count": len(ov_rows),
    }
    if total > 0:
        logger.info(
            "매수 미결제 −%s원 (국내 T+2 %s + 해외 T+1 %s)",
            f"{total:,.0f}",
            f"{dom_krw:,.0f}",
            f"{ov_krw:,.0f}",
        )
    return total, detail


def parse_domestic_liabilities_krw(domestic_output2: Optional[dict]) -> dict:
    """국내 output2 부채·미수 항목 (원화)."""
    o = domestic_output2 if isinstance(domestic_output2, dict) else {}
    nrcvb = _kis_float_o2(o, "nrcvb_buy_amt")
    tot_loan = _kis_float_o2(o, "tot_loan_amt")
    stln = _kis_float_o2(o, "tot_stln_slng_chgs")
    return {
        "nrcvb_buy_amt_krw": round(nrcvb),
        "credit_loan_krw": round(tot_loan),
        "stock_loan_charges_krw": round(stln),
        "total_deduction_krw": round(nrcvb + tot_loan),
    }


def merge_trade_sources(*sources: list[dict]) -> list[dict]:
    """ticker·거래일·금액 기준 중복 제거 후 병합."""
    seen: set[tuple] = set()
    merged: list[dict] = []
    for trades in sources:
        for tr in trades:
            t_date = trade_calendar_date(tr) or ""
            key = (
                (tr.get("ticker") or "").upper(),
                tr.get("type"),
                t_date,
                round(float(tr.get("amount") or 0), 2),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(tr)
    return merged


def calc_pending_sell_proceeds_krw(
    holdings: list[dict],
    trades: list[dict],
    usd_krw_rate: Optional[float],
    now_kst: Optional[datetime] = None,
    *,
    domestic_output2: Optional[dict] = None,
    overseas=None,
) -> tuple[float, list[dict]]:
    """
    미반영 현금 보정 (하위 호환 이름).
    holdings 인자는 사용하지 않음.
    """
    del holdings
    adjustment, detail = calc_unreflected_cash_adjustment_krw(
        trades,
        domestic_output2,
        usd_krw_rate,
        now_kst,
        overseas=overseas,
    )
    details = [detail] if adjustment > 0 or detail.get("today_sell_krw", 0) > 0 else []
    return adjustment, details


def _domestic_holdings_by_ticker(holdings: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for h in holdings or []:
        if h.get("currency") == "USD":
            continue
        t = (h.get("ticker") or "").strip()
        if t:
            out[t] = h
    return out


def merge_today_domestic_buys_into_holdings(
    holdings: list[dict],
    trades: list[dict],
    get_price,
) -> list[dict]:
    """
    당일 국내 BUY 체결 중 잔고(output1)에 없는 종목을 보유 목록에 추가.
    get_price: ticker -> 현재가(원)
    """
    by_ticker = _domestic_holdings_by_ticker(holdings)
    buy_qty: dict[str, int] = {}
    buy_amt: dict[str, float] = {}
    buy_name: dict[str, str] = {}
    buy_price: dict[str, float] = {}

    for tr in trades or []:
        if tr.get("type") != "BUY":
            continue
        if tr.get("region") == "US":
            continue
        if not is_trade_on_business_today(tr):
            continue
        ticker = (tr.get("ticker") or "").strip()
        qty = int(tr.get("quantity") or 0)
        if not ticker or qty <= 0:
            continue
        buy_qty[ticker] = buy_qty.get(ticker, 0) + qty
        buy_amt[ticker] = buy_amt.get(ticker, 0.0) + float(tr.get("amount") or 0)
        buy_name.setdefault(ticker, tr.get("name") or ticker)
        if tr.get("price"):
            buy_price[ticker] = float(tr["price"])

    added: list[str] = []
    for ticker, qty in buy_qty.items():
        if ticker in by_ticker and int(by_ticker[ticker].get("quantity") or 0) > 0:
            continue

        avg = buy_amt[ticker] / qty if buy_amt.get(ticker, 0) > 0 else buy_price.get(ticker, 0)
        try:
            current = float(get_price(ticker) or 0)
        except Exception:
            current = 0.0
        if current <= 0:
            current = buy_price.get(ticker) or avg
        eval_amount = current * qty if current > 0 else buy_amt.get(ticker, 0)
        purchase_amount = buy_amt.get(ticker, 0) or (avg * qty if avg > 0 else 0)
        profit_rate = (
            (eval_amount - purchase_amount) / purchase_amount * 100
            if purchase_amount > 0
            else 0.0
        )
        row = {
            "ticker": ticker,
            "name": buy_name.get(ticker, ticker),
            "quantity": qty,
            "avg_price": avg,
            "current_price": current,
            "profit_rate": profit_rate,
            "eval_amount": eval_amount,
            "purchase_amount": purchase_amount,
            "currency": "KRW",
            "source": "today_buy_ccld",
        }
        by_ticker[ticker] = row
        added.append(ticker)

    if added:
        logger.info("당일 매수 잔고 보완 추가: %s", ", ".join(added))

    usd = [h for h in holdings if h.get("currency") == "USD"]
    return list(by_ticker.values()) + usd


def calc_pending_buy_eval_krw(
    holdings: list[dict],
    trades: list[dict],
    get_price=None,
    now_kst: Optional[datetime] = None,
) -> tuple[float, list[dict]]:
    """
    당일 국내 BUY 체결 중 보유 잔고에 전혀 없는 종목 평가액(원화).
    merge_today_domestic_buys_into_holdings 실패 시 총자산 보정용.
    """
    by_ticker = _domestic_holdings_by_ticker(holdings)
    total_krw = 0.0
    details: list[dict] = []

    buy_qty: dict[str, int] = {}
    buy_amt: dict[str, float] = {}
    for tr in trades or []:
        if tr.get("type") != "BUY" or tr.get("region") == "US":
            continue
        if not is_trade_on_business_today(tr, now_kst):
            continue
        ticker = (tr.get("ticker") or "").strip()
        qty = int(tr.get("quantity") or 0)
        if not ticker or qty <= 0:
            continue
        buy_qty[ticker] = buy_qty.get(ticker, 0) + qty
        buy_amt[ticker] = buy_amt.get(ticker, 0.0) + float(tr.get("amount") or 0)

    for ticker, qty in buy_qty.items():
        if ticker in by_ticker and int(by_ticker[ticker].get("quantity") or 0) > 0:
            continue
        amt = buy_amt.get(ticker, 0.0)
        if get_price:
            try:
                pr = float(get_price(ticker) or 0)
                if pr > 0:
                    amt = pr * qty
            except Exception:
                pass
        if amt <= 0:
            continue
        total_krw += round(amt)
        details.append({"ticker": ticker, "quantity": qty, "eval_krw": round(amt)})

    if details:
        logger.info(
            "잔고 미반영 매수 보정 +%s원 (%d건)",
            f"{total_krw:,.0f}",
            len(details),
        )
    return total_krw, details
