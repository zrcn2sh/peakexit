"""
자동 매도 로직 엔진

매도 조건 3가지:
1. 손절 (Stop Loss)      : 수익률이 설정값 이하 → 즉시 매도
2. 목표가 (Take Profit)  : 수익률이 목표값 이상 → 즉시 매도 (선택)
3. 트레일링 스탑
   └ 발동%(trigger) 이상 상승 후: 고점 대비 trailing_drop% 하락 시 매도
   └ 발동 전: 매입가 대비 trailing_drop% 하락 시 매도
   └ 종목별 AI 분석으로 트레일링 % 자동 결정
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.core.paths import get_data_dir
from app.services.kis_client import get_kis_client, _kis_float

logger = logging.getLogger(__name__)


def _peak_file() -> Path:
    return get_data_dir() / "peak_tracker.json"

DEFAULT_SETTINGS = {
    "stop_loss_pct": -10.0,
    "trailing_trigger_pct": 5.0,
    "trailing_drop_pct": 5.0,
    "take_profit_pct": None,
    "enabled": True,
}


# ─────────────────────────────────────────────────
# 고점 추적기
# ─────────────────────────────────────────────────
class PeakTracker:
    def __init__(self):
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self):
        pf = _peak_file()
        pf.parent.mkdir(parents=True, exist_ok=True)
        if pf.exists():
            try:
                self.data = json.loads(pf.read_text())
            except Exception:
                self.data = {}

    def _save(self):
        _peak_file().write_text(json.dumps(self.data, ensure_ascii=False, indent=2))

    def update(self, ticker: str, current_price: float, avg_price: float) -> dict:
        """
        고점 추적.
        - 초기/하한 고점 = 매입평균(avg_price). 매수 직후 하락도 '고점=매입가' 기준.
        - 이후 현재가가 더 높으면 고점 갱신.
        """
        if avg_price <= 0 or current_price < 0:
            return {
                "peak_price": current_price,
                "peak_profit_rate": 0.0,
                "peak_at": datetime.now().isoformat(),
                "drop_from_peak_pct": 0.0,
            }

        profit_rate = (current_price - avg_price) / avg_price * 100
        # 매입가를 최소 고점으로 — 관측 시작이 이미 손실이어도 매입 시점을 고점으로 본다.
        floor_peak = float(avg_price)

        if ticker not in self.data:
            peak = max(current_price, floor_peak)
            self.data[ticker] = {
                "peak_price": peak,
                "peak_profit_rate": (peak - avg_price) / avg_price * 100,
                "peak_at": datetime.now().isoformat(),
                "avg_price": avg_price,
            }
        else:
            entry = self.data[ticker]
            prev_peak = float(entry.get("peak_price") or 0)
            # 과거 데이터가 매입가보다 낮게 잡혀 있으면 매입가로 보정
            peak = max(prev_peak, floor_peak, current_price)
            if peak > prev_peak + 1e-12:
                entry["peak_price"] = peak
                entry["peak_profit_rate"] = (peak - avg_price) / avg_price * 100
                entry["peak_at"] = datetime.now().isoformat()
            entry["avg_price"] = avg_price

        self._save()
        entry = self.data[ticker]
        peak_price = float(entry["peak_price"])
        drop_from_peak = (current_price - peak_price) / peak_price * 100 if peak_price > 0 else 0.0
        return {
            "peak_price": peak_price,
            "peak_profit_rate": float(entry.get("peak_profit_rate") or 0),
            "peak_at": entry.get("peak_at"),
            "drop_from_peak_pct": drop_from_peak,
        }

    def remove(self, ticker: str):
        self.data.pop(ticker, None)
        self._save()


peak_tracker = PeakTracker()


def _trading_meta_from_holding(holding: dict) -> dict:
    """해외 매도 시 필요한 거래소 코드 등 (스케줄러에서 보강된 필드)."""
    meta = {}
    if holding.get("ovrs_excg_cd"):
        meta["ovrs_excg_cd"] = holding["ovrs_excg_cd"]
    if holding.get("ovrs_quote_excd"):
        meta["ovrs_quote_excd"] = holding["ovrs_quote_excd"]
    if holding.get("currency"):
        meta["currency"] = holding["currency"]
    return meta


def enrich_sell_execution_details(payload: dict) -> dict:
    """매도 로그·텔레그램용: 매입/매도 금액·수익 합계·원화 환산 필드 보강."""
    qty = int(payload.get("quantity") or 0)
    ccy = payload.get("currency") or ("USD" if payload.get("ovrs_excg_cd") else "KRW")
    dec = 2 if ccy == "USD" else 0

    raw_avg = payload.get("avg_price")
    has_avg = False
    avg = 0.0
    if raw_avg is not None and raw_avg != "":
        try:
            avg = float(raw_avg)
            has_avg = avg > 0
        except (TypeError, ValueError):
            pass

    sell = 0.0
    if payload.get("sell_price") is not None:
        try:
            sell = float(payload["sell_price"])
        except (TypeError, ValueError):
            pass
    elif payload.get("current_price") is not None:
        try:
            sell = float(payload["current_price"])
        except (TypeError, ValueError):
            pass
    has_sell = sell > 0 and qty > 0

    purchase_total = round(avg * qty, dec) if has_avg else None
    sell_total = round(sell * qty, dec) if has_sell else None
    profit_amount = (
        round(sell_total - purchase_total, dec)
        if has_avg and sell_total is not None and purchase_total is not None
        else None
    )

    profit_rate = payload.get("profit_rate")
    try:
        profit_rate = float(profit_rate) if profit_rate is not None else None
    except (TypeError, ValueError):
        profit_rate = None
    if profit_rate is None and purchase_total and purchase_total > 0 and sell_total is not None:
        profit_rate = (sell_total - purchase_total) / purchase_total * 100

    out = {
        **payload,
        "currency": ccy,
        "avg_price": avg if has_avg else None,
        "sell_price": sell if sell > 0 else None,
        "purchase_total": purchase_total,
        "sell_total": sell_total,
        "profit_amount": profit_amount,
        "profit_rate": float(profit_rate) if profit_rate is not None else None,
    }

    if ccy == "USD":
        from app.services.fx_rate import get_usd_krw_rate

        rate = get_usd_krw_rate()
        out["usd_krw_rate"] = rate
        if rate and sell_total is not None:
            out["sell_total_krw"] = round(sell_total * rate)
        if rate and profit_amount is not None:
            out["profit_amount_krw"] = round(profit_amount * rate)
        if rate and purchase_total is not None:
            out["purchase_total_krw"] = round(purchase_total * rate)
    else:
        if sell_total is not None:
            out["sell_total_krw"] = int(sell_total)
        if profit_amount is not None:
            out["profit_amount_krw"] = int(profit_amount)
        if purchase_total is not None:
            out["purchase_total_krw"] = int(purchase_total)

    return out


def _fetch_live_price_for_log_entry(client, entry: dict) -> Optional[float]:
    ticker = (entry.get("ticker") or "").strip()
    if not ticker:
        return None
    try:
        if entry.get("ovrs_excg_cd") or (entry.get("currency") or "") == "USD":
            qcd = entry.get("ovrs_quote_excd")
            if not qcd:
                _, qcd = client.detect_us_exchange(ticker)
            px = client.get_overseas_current_price(ticker, qcd)
        else:
            px = client.get_current_price(ticker)
        return float(px) if px and float(px) > 0 else None
    except Exception as e:
        logger.debug("매도이력 현재가 조회 실패 %s: %s", ticker, e)
        return None


def enrich_sell_log_with_live_prices(entries: list[dict]) -> list[dict]:
    """
    매도 이력에 현재가·매도가 대비 등락% 를 붙인다.
    동일 티커는 1회만 시세 조회.
    """
    if not entries:
        return []
    client = get_kis_client()
    price_cache: dict[str, Optional[float]] = {}
    out: list[dict] = []
    for raw in entries:
        entry = enrich_sell_execution_details(raw)
        ticker = (entry.get("ticker") or "").strip().upper()
        key = f"{ticker}:{(entry.get('ovrs_excg_cd') or entry.get('currency') or 'KRW')}"
        if key not in price_cache:
            price_cache[key] = _fetch_live_price_for_log_entry(client, entry) if ticker else None
        live = price_cache[key]
        sell = entry.get("sell_price")
        try:
            sell_f = float(sell) if sell is not None else 0.0
        except (TypeError, ValueError):
            sell_f = 0.0
        vs_pct = None
        if live is not None and sell_f > 0:
            vs_pct = round((live - sell_f) / sell_f * 100, 2)
        entry["live_price"] = live
        entry["vs_sell_pct"] = vs_pct
        out.append(entry)
    return out


# ─────────────────────────────────────────────────
# 매도 판단 엔진
# ─────────────────────────────────────────────────
def evaluate_sell(
    holding: dict,
    settings: dict,
    trailing_config: Optional[dict] = None,
) -> Optional[dict]:
    """
    holding: {ticker, name, quantity, avg_price, current_price, profit_rate, ...}
    settings: 전역 매도 조건 설정값
    trailing_config: AI가 판단한 종목별 트레일링 설정 (없으면 전역값 사용)
    """
    ticker = holding["ticker"]
    name = holding["name"]
    qty = holding["quantity"]
    avg_price = holding["avg_price"]
    current_price = holding["current_price"]
    profit_rate = holding["profit_rate"]

    if qty <= 0 or avg_price <= 0 or current_price < 0:
        logger.warning(
            f"{ticker}: 매도 판단 스킵 (비정상 보유/가격 qty={qty} avg_price={avg_price} current_price={current_price})"
        )
        return None

    stop_loss = settings.get("stop_loss_pct", DEFAULT_SETTINGS["stop_loss_pct"])
    take_profit = settings.get("take_profit_pct", DEFAULT_SETTINGS["take_profit_pct"])

    # 종목별 AI 트레일링 설정 우선, 없으면 전역 설정 사용
    if trailing_config:
        trailing_trigger = trailing_config.get("trailing_trigger_pct", DEFAULT_SETTINGS["trailing_trigger_pct"])
        trailing_drop = trailing_config.get("trailing_drop_pct", DEFAULT_SETTINGS["trailing_drop_pct"])
        trailing_source = f"AI추천({trailing_config.get('confidence','?')}) {trailing_drop}%"
        classification = trailing_config.get("classification", {})
        atr_pct = trailing_config.get("atr_pct")
    else:
        trailing_trigger = settings.get("trailing_trigger_pct", DEFAULT_SETTINGS["trailing_trigger_pct"])
        trailing_drop = settings.get("trailing_drop_pct", DEFAULT_SETTINGS["trailing_drop_pct"])
        trailing_source = f"전역설정 {trailing_drop}%"
        classification = {}
        atr_pct = None

    # ── 1. 손절 ────────────────────────────────
    if profit_rate <= stop_loss:
        return {
            "ticker": ticker,
            "name": name,
            "quantity": qty,
            "reason": "STOP_LOSS",
            "reason_label": f"손절 ({profit_rate:.2f}% ≤ {stop_loss}%)",
            "profit_rate": profit_rate,
            "avg_price": avg_price,
            "current_price": current_price,
            "trailing_source": trailing_source,
            **_trading_meta_from_holding(holding),
        }

    # ── 2. 목표가 즉시 매도 (선택) ─────────────
    if take_profit is not None and profit_rate >= take_profit:
        return {
            "ticker": ticker,
            "name": name,
            "quantity": qty,
            "reason": "TAKE_PROFIT",
            "reason_label": f"목표가 달성 ({profit_rate:.2f}% ≥ {take_profit}%)",
            "profit_rate": profit_rate,
            "avg_price": avg_price,
            "current_price": current_price,
            "trailing_source": trailing_source,
            **_trading_meta_from_holding(holding),
        }

    # ── 3. 트레일링 스탑 ──────────────────────────
    # - 고점 수익률 ≥ 발동%(trigger): 고점 대비 trailing_drop% 하락 시 ("어깨")
    # - 아직 발동 전(고점이 매입가 근처): 매입가 대비 trailing_drop% 하락 시
    #   (고점이 매입가보다 아주 조금만 높아도 발동 미달이면 매입가 기준으로 보호)
    peak_info = peak_tracker.update(ticker, current_price, avg_price)
    drop_from_peak = peak_info["drop_from_peak_pct"]
    peak_profit = peak_info["peak_profit_rate"]

    if peak_profit >= trailing_trigger:
        trailing_hit = drop_from_peak <= -trailing_drop
        trail_basis = f"고점 {peak_profit:.1f}%에서 {drop_from_peak:.1f}% 하락"
    else:
        trailing_hit = profit_rate <= -trailing_drop
        trail_basis = f"매입가 대비 {profit_rate:.1f}% (발동 전 보호)"

    if trailing_hit:
        return {
            "ticker": ticker,
            "name": name,
            "quantity": qty,
            "reason": "TRAILING_STOP",
            "reason_label": (
                f"트레일링 스탑 ({trail_basis} / 기준: {trailing_source})"
            ),
            "profit_rate": profit_rate,
            "avg_price": avg_price,
            "current_price": current_price,
            "peak_price": peak_info["peak_price"],
            "peak_profit_rate": peak_profit,
            "drop_from_peak_pct": drop_from_peak,
            "trailing_drop_applied": trailing_drop,
            "trailing_source": trailing_source,
            "classification": classification,
            "atr_pct": atr_pct,
            **_trading_meta_from_holding(holding),
        }

    return None


# ─────────────────────────────────────────────────
# 실제 매도 실행
# ─────────────────────────────────────────────────
def execute_sell(signal: dict) -> dict:
    client = get_kis_client()
    ticker = signal["ticker"]
    qty = signal["quantity"]
    sell_price = float(signal.get("sell_price") or signal.get("current_price") or 0)

    try:
        if signal.get("ovrs_excg_cd"):
            result, sell_price = client.sell_overseas_market_order(
                ticker,
                qty,
                ovrs_excg_cd=signal["ovrs_excg_cd"],
                quote_excd=signal.get("ovrs_quote_excd"),
            )
            out = result.get("output") or {}
            order_no = out.get("odno") or out.get("ODNO") or "N/A"
        else:
            result = client.sell_market_order(ticker, qty)
            order_no = result.get("output", {}).get("odno", "N/A")
        peak_tracker.remove(ticker)
        logger.info(f"[매도완료] {ticker} {qty}주 주문번호={order_no}")
        return enrich_sell_execution_details({
            "success": True,
            "order_no": order_no,
            "message": f"{signal['name']} {qty}주 매도 완료",
            **signal,
            "sell_price": sell_price,
        })
    except Exception as e:
        logger.error(f"[매도실패] {ticker}: {e}")
        return enrich_sell_execution_details({
            "success": False,
            "error": str(e),
            "message": f"{signal['name']} 매도 실패: {e}",
            **signal,
            "sell_price": sell_price,
        })


# ─────────────────────────────────────────────────
# 포트폴리오 수익률 계산
# ─────────────────────────────────────────────────
def calc_portfolio_summary(
    holdings: list[dict],
    seed_money: float,
    trade_history: Optional[list[dict]] = None,
    domestic_balance_output2: Optional[dict] = None,
    *,
    fetch_trades_for_adjustment: bool = True,
) -> dict:
    """
    총자산·시드 수익률:
    - 국내주식 + 국내예수금 + 해외주식(API evlu_amt 원화) + 해외예수금
    - (+) 매도 미결제(T+2 국내 / T+1 해외) − (−) 매수 미결제(T+2/T+1)
    - (−) 미수매수(nrcvb_buy_amt) · 신용대출(tot_loan_amt)
    - 수익률(시드) = (총자산 - 시드) / 시드 × 100
    """
    from app.core.portfolio_adjustment import (
        calc_pending_buy_eval_krw,
        calc_pending_buy_settlement_krw,
        calc_pending_sell_settlement_krw,
        estimate_domestic_settlement_payable_krw,
        estimate_domestic_settlement_receivable_krw,
        estimate_today_domestic_buy_krw,
        merge_pending_buy_with_today_domestic,
        merge_pending_sell_with_domestic_receivable,
        merge_trade_sources,
        parse_domestic_liabilities_krw,
        pending_settlement_mode,
        resolve_account_base_krw,
        trades_from_sell_log,
    )
    from app.services.fx_rate import get_usd_krw_rate

    o = domestic_balance_output2 if isinstance(domestic_balance_output2, dict) else {}
    tot_evlu_amt = _kis_float(o, "tot_evlu_amt")
    nass_amt = _kis_float(o, "nass_amt")
    cash_deposit_krw = _kis_float(o, "dnca_tot_amt")
    scts_evlu_amt = _kis_float(o, "scts_evlu_amt")
    output2_purchase = _kis_float(o, "pchs_amt_smtl_amt")
    output2_unrealized = _kis_float(o, "evlu_pfls_smtl_amt")

    kr = [h for h in holdings if h.get("currency", "KRW") != "USD"]
    usd = [h for h in holdings if h.get("currency") == "USD"]

    holdings_eval_krw = sum(h["eval_amount"] for h in kr)
    holdings_purchase_krw = sum(h["purchase_amount"] for h in kr)
    holdings_eval_usd = sum(h["eval_amount"] for h in usd)
    holdings_purchase_usd = sum(h["purchase_amount"] for h in usd)

    # 매도 보정용 환율만 조회 (총자산 합산에는 미사용)
    fx_rate = get_usd_krw_rate()

    client = get_kis_client()

    try:
        client.refresh_overseas_account_valuation()
    except Exception as e:
        logger.warning("해외 총자산 조회 실패: %s", e)

    ov_v = client.get_last_overseas_valuation()

    trades = list(trade_history) if trade_history else []
    if fetch_trades_for_adjustment:
        try:
            api_trades = client.get_combined_trade_history(days=14)
        except Exception as e:
            logger.warning("체결내역 API 조회 실패: %s", e)
            api_trades = []
        log_trades = trades_from_sell_log(max_days=14)
        trades = merge_trade_sources(log_trades, api_trades, trades)

    pending_sell_gross_krw, settle_detail = calc_pending_sell_settlement_krw(trades, fx_rate)
    pending_buy_settle_krw, buy_settle_detail = calc_pending_buy_settlement_krw(trades, fx_rate)
    today_dom_buy_krw, today_dom_detail = estimate_today_domestic_buy_krw(o, holdings)
    from app.core.account_valuation import holdings_domestic_stocks_krw as _dom_stocks_sum

    dom_stocks_hint = _dom_stocks_sum(holdings or [])
    if dom_stocks_hint <= 0:
        dom_stocks_hint = scts_evlu_amt
    receivable_krw, receivable_detail = estimate_domestic_settlement_receivable_krw(
        o, dom_stocks_hint, cash_deposit_krw
    )
    ov_ustl_sll_krw = float(getattr(ov_v, "ustl_sll_amt_krw", 0) or 0)
    ov_ustl_sll_src = ""
    if isinstance(getattr(ov_v, "detail", None), dict):
        ov_ustl_sll_src = str(ov_v.detail.get("ustl_sll_source") or "")
        if ov_ustl_sll_krw <= 0:
            ov_ustl_sll_krw = float(ov_v.detail.get("ustl_sll_amt_krw") or 0)
    pending_sell_gross_krw, settle_detail = merge_pending_sell_with_domestic_receivable(
        pending_sell_gross_krw,
        settle_detail,
        settlement_receivable_krw=receivable_krw,
        settlement_receivable_detail=receivable_detail,
        overseas_ustl_sll_krw=ov_ustl_sll_krw,
        overseas_ustl_sll_source=ov_ustl_sll_src,
    )
    payable_krw, payable_detail = estimate_domestic_settlement_payable_krw(
        o, dom_stocks_hint, cash_deposit_krw
    )
    ov_ustl_krw = float(getattr(ov_v, "ustl_buy_amt_krw", 0) or 0)
    ov_ustl_src = ""
    if isinstance(getattr(ov_v, "detail", None), dict):
        ov_ustl_src = str(ov_v.detail.get("ustl_buy_source") or "")
        if ov_ustl_krw <= 0:
            ov_ustl_krw = float(ov_v.detail.get("ustl_buy_amt_krw") or 0)
    pending_buy_settle_krw, buy_settle_detail = merge_pending_buy_with_today_domestic(
        pending_buy_settle_krw,
        buy_settle_detail,
        today_dom_buy_krw,
        today_dom_detail,
        settlement_payable_krw=payable_krw,
        settlement_payable_detail=payable_detail,
        overseas_ustl_buy_krw=ov_ustl_krw,
        overseas_ustl_source=ov_ustl_src,
    )
    pending_buy_eval_krw, pending_buy_eval_details = calc_pending_buy_eval_krw(holdings, trades)
    liabilities = parse_domestic_liabilities_krw(o)

    try:
        valuation_snapshot = client.get_account_valuation_snapshot(o)
    except Exception as e:
        logger.warning("계좌 평가 스냅샷 실패: %s", e)
        valuation_snapshot = None

    from app.core.account_valuation import DomesticValuation, build_asset_breakdown

    base_info = resolve_account_base_krw(
        o,
        client.get_last_overseas_balance_eval_krw(),
        holdings,
        valuation_snapshot=valuation_snapshot,
    )
    account_base_method = base_info["account_base_method"]
    uses_output2_tot = tot_evlu_amt > 0

    if valuation_snapshot is not None:
        dom_v = valuation_snapshot.domestic
        ov_v = valuation_snapshot.overseas
    else:
        dom_v = DomesticValuation.from_output2(o)
        ov_v = client.get_last_overseas_valuation()

    breakdown = build_asset_breakdown(
        dom_v,
        ov_v,
        holdings,
        fx_rate,
        pending_sell_settlement_krw=pending_sell_gross_krw,
        pending_sell_settlement_dom_krw=float(settle_detail.get("domestic_krw") or 0),
        pending_sell_settlement_ov_krw=float(settle_detail.get("overseas_krw") or 0),
        pending_buy_settlement_krw=pending_buy_settle_krw,
        pending_buy_settlement_dom_krw=float(buy_settle_detail.get("domestic_krw") or 0),
        pending_buy_settlement_ov_krw=float(buy_settle_detail.get("overseas_krw") or 0),
        pending_buy_krw=pending_buy_eval_krw,
        nrcvb_buy_amt_krw=float(liabilities.get("nrcvb_buy_amt_krw") or 0),
        credit_loan_krw=float(liabilities.get("credit_loan_krw") or 0),
        domestic_output2=o,
    )
    account_base_krw = float(breakdown["subtotal_krw"])
    total_net_worth_krw = float(breakdown["total_net_worth_krw"])
    base_info["account_base_krw"] = account_base_krw
    base_info["domestic_base_krw"] = breakdown["domestic_stocks_krw"] + breakdown["domestic_cash_krw"]
    base_info["overseas_base_krw"] = breakdown["overseas_stocks_krw"] + breakdown["overseas_cash_krw"]
    base_info["asset_breakdown"] = breakdown

    purchase_usd_as_krw = (
        round(holdings_purchase_usd * fx_rate) if fx_rate and holdings_purchase_usd > 0 else 0.0
    )
    stocks_eval_for_cost = float(breakdown["domestic_stocks_krw"]) + float(
        breakdown["overseas_stocks_krw"]
    )

    # output2 매입/손익은 국내(TTTC8434R)만 — 해외 보유 시 보유 합산과 병합
    if output2_purchase > 0 and not usd:
        dom_purchase_krw = output2_purchase
        total_purchase = dom_purchase_krw
        unrealized_pnl = output2_unrealized
        if unrealized_pnl == 0 and scts_evlu_amt > 0:
            unrealized_pnl = scts_evlu_amt - output2_purchase
    elif output2_purchase > 0 and usd:
        dom_purchase_krw = output2_purchase
        total_purchase = dom_purchase_krw + purchase_usd_as_krw
        unrealized_pnl = stocks_eval_for_cost - total_purchase
    else:
        dom_purchase_krw = holdings_purchase_krw
        total_purchase = dom_purchase_krw + purchase_usd_as_krw
        if stocks_eval_for_cost > 0:
            unrealized_pnl = stocks_eval_for_cost - total_purchase
        else:
            eval_usd_as_krw_fb = (holdings_eval_usd * fx_rate) if fx_rate else 0.0
            current_eval_fb = holdings_eval_krw + eval_usd_as_krw_fb
            unrealized_pnl = current_eval_fb - total_purchase

    return_on_cost_pct = (
        (unrealized_pnl / total_purchase * 100) if total_purchase > 0 else 0.0
    )
    total_return_pct = (
        ((total_net_worth_krw - seed_money) / seed_money * 100) if seed_money > 0 else 0.0
    )

    stocks_eval_total = float(base_info["stocks_eval_total_krw"])

    return {
        "seed_money": seed_money,
        "current_eval": account_base_krw,
        "current_eval_adjusted": total_net_worth_krw,
        "account_tot_evlu_krw": account_base_krw,
        "account_base_method": account_base_method,
        "stocks_eval_total_krw": stocks_eval_total,
        "stocks_eval_domestic_krw": float(breakdown["domestic_stocks_krw"]),
        "stocks_eval_overseas_krw": float(breakdown["overseas_stocks_krw"]),
        "overseas_eval_from_api_krw": float(base_info.get("overseas_eval_from_api_krw") or 0),
        "overseas_usd_cash_krw": float(base_info.get("overseas_usd_cash_krw") or 0),
        "overseas_valuation_source": base_info.get("overseas_valuation_source", ""),
        "domestic_tot_evlu_component_krw": float(base_info.get("domestic_tot_evlu_krw") or 0),
        "domestic_base_krw": float(base_info.get("domestic_base_krw") or 0),
        "overseas_base_krw": float(base_info.get("overseas_base_krw") or 0),
        "asset_breakdown": breakdown,
        "domestic_stocks_krw": breakdown["domestic_stocks_krw"],
        "domestic_cash_krw": breakdown["domestic_cash_krw"],
        "overseas_stocks_usd": breakdown["overseas_stocks_usd"],
        "overseas_stocks_krw": breakdown["overseas_stocks_krw"],
        "overseas_cash_usd": breakdown["overseas_cash_usd"],
        "overseas_cash_krw": breakdown["overseas_cash_krw"],
        "asset_subtotal_krw": breakdown["subtotal_krw"],
        "pending_sell_settlement_krw": breakdown.get("pending_settlement_net_krw", breakdown.get("pending_sell_settlement_krw", 0)),
        "pending_sell_settlement_gross_krw": breakdown.get("pending_sell_settlement_gross_krw", 0),
        "pending_sell_settlement_dom_krw": breakdown.get("pending_sell_settlement_dom_krw", 0),
        "pending_sell_settlement_ov_krw": breakdown.get("pending_sell_settlement_ov_krw", 0),
        "pending_buy_settlement_krw": breakdown.get("pending_buy_settlement_krw", 0),
        "pending_buy_settlement_dom_krw": breakdown.get("pending_buy_settlement_dom_krw", 0),
        "pending_buy_settlement_ov_krw": breakdown.get("pending_buy_settlement_ov_krw", 0),
        "pending_settlement_net_krw": breakdown.get("pending_settlement_net_krw", 0),
        "nrcvb_buy_amt_krw": breakdown.get("nrcvb_buy_amt_krw", 0),
        "credit_loan_krw": breakdown.get("credit_loan_krw", 0),
        "deductions_krw": breakdown.get("deductions_krw", 0),
        "total_net_worth_krw": total_net_worth_krw,
        "current_eval_krw_domestic": float(breakdown["domestic_stocks_krw"]),
        "current_eval_usd": holdings_eval_usd,
        "current_eval_usd_as_krw": None,
        "total_purchase": total_purchase,
        "total_purchase_krw_domestic": dom_purchase_krw,
        "purchase_amount_usd_total": holdings_purchase_usd,
        "purchase_amount_usd_as_krw": purchase_usd_as_krw if purchase_usd_as_krw > 0 else None,
        "cost_basis_includes_overseas": bool(usd),
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_krw_domestic": output2_unrealized if uses_output2_tot else (holdings_eval_krw - holdings_purchase_krw),
        "unrealized_pnl_usd": holdings_eval_usd - holdings_purchase_usd,
        "unrealized_pnl_usd_as_krw": None,
        "cash_deposit_krw": cash_deposit_krw,
        "domestic_nass_krw": nass_amt,
        "domestic_tot_evlu_krw": tot_evlu_amt,
        "domestic_scts_evlu_krw": scts_evlu_amt,
        "output2_purchase_krw": output2_purchase,
        "output2_unrealized_krw": output2_unrealized,
        "seed_basis_includes_cash": True,
        "seed_basis_from_output2": uses_output2_tot,
        "pnl_vs_seed_krw": total_net_worth_krw - seed_money,
        "return_pct_on_seed": total_return_pct,
        "return_pct_on_holdings_cost": return_on_cost_pct,
        "return_on_cost_pct": return_on_cost_pct,
        "total_return_pct": total_return_pct,
        "estimated_balance": total_net_worth_krw,
        "pending_sell_proceeds_krw": breakdown.get("pending_settlement_net_krw", 0),
        "pending_cash_adjustment_krw": breakdown.get("pending_settlement_net_krw", 0),
        "pending_sell_adjustments": [settle_detail] if settle_detail.get("total_krw") else [],
        "pending_sell_settlement_detail": settle_detail,
        "pending_buy_settlement_detail": buy_settle_detail,
        "liabilities_detail": liabilities,
        "hts_tot_evlu_krw": tot_evlu_amt,
        "hts_nass_krw": nass_amt,
        "pending_buy_eval_krw": pending_buy_eval_krw,
        "pending_buy_adjustments": pending_buy_eval_details,
        "pending_sell_settlement_mode": pending_settlement_mode(),
        "pending_sell_trade_sources": len(trades),
        "holdings_count": len(holdings),
        "holdings_count_domestic": len(kr),
        "holdings_count_overseas": len(usd),
        "has_overseas": len(usd) > 0,
        "usd_krw_rate": fx_rate,
        "fx_includes_usd": False,
        "fx_usd_excluded_from_krw_totals": False,
    }
