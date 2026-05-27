"""
자동 매도 로직 엔진

매도 조건 3가지:
1. 손절 (Stop Loss)      : 수익률이 설정값 이하 → 즉시 매도
2. 목표가 (Take Profit)  : 수익률이 목표값 이상 → 즉시 매도 (선택)
3. 트레일링 스탑         : 고점 대비 N% 하락 → 매도 ("어깨에서 팔기")
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
        if avg_price <= 0 or current_price < 0:
            return {
                "peak_price": current_price,
                "peak_profit_rate": 0.0,
                "peak_at": datetime.now().isoformat(),
                "drop_from_peak_pct": 0.0,
            }

        profit_rate = (current_price - avg_price) / avg_price * 100

        if ticker not in self.data:
            self.data[ticker] = {
                "peak_price": current_price,
                "peak_profit_rate": profit_rate,
                "peak_at": datetime.now().isoformat(),
                "avg_price": avg_price,
            }
        else:
            if current_price > self.data[ticker]["peak_price"]:
                self.data[ticker]["peak_price"] = current_price
                self.data[ticker]["peak_profit_rate"] = profit_rate
                self.data[ticker]["peak_at"] = datetime.now().isoformat()

        self._save()
        entry = self.data[ticker]
        drop_from_peak = (current_price - entry["peak_price"]) / entry["peak_price"] * 100
        return {
            "peak_price": entry["peak_price"],
            "peak_profit_rate": entry["peak_profit_rate"],
            "peak_at": entry["peak_at"],
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
            "current_price": current_price,
            "trailing_source": trailing_source,
            **_trading_meta_from_holding(holding),
        }

    # ── 3. 트레일링 스탑 ("어깨에서 팔기") ──────
    peak_info = peak_tracker.update(ticker, current_price, avg_price)
    drop_from_peak = peak_info["drop_from_peak_pct"]
    peak_profit = peak_info["peak_profit_rate"]

    if peak_profit >= trailing_trigger and drop_from_peak <= -trailing_drop:
        return {
            "ticker": ticker,
            "name": name,
            "quantity": qty,
            "reason": "TRAILING_STOP",
            "reason_label": (
                f"트레일링 스탑 (고점 {peak_profit:.1f}%에서 "
                f"{drop_from_peak:.1f}% 하락 / 기준: {trailing_source})"
            ),
            "profit_rate": profit_rate,
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

    try:
        if signal.get("ovrs_excg_cd"):
            result = client.sell_overseas_market_order(
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
        return {
            "success": True,
            "order_no": order_no,
            "message": f"{signal['name']} {qty}주 매도 완료",
            **signal,
        }
    except Exception as e:
        logger.error(f"[매도실패] {ticker}: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": f"{signal['name']} 매도 실패: {e}",
            **signal,
        }


# ─────────────────────────────────────────────────
# 포트폴리오 수익률 계산
# ─────────────────────────────────────────────────
def calc_portfolio_summary(
    holdings: list[dict],
    seed_money: float,
    trade_history: list[dict],
    domestic_balance_output2: Optional[dict] = None,
) -> dict:
    """
    시드 대비 수익률(총자산 기준):
    - 국내: 잔고조회 output2 의 순자산(nass_amt) 또는 총평가(tot_evlu_amt)가 있으면
      그 값(예수금·주식 등 계좌 합산에 가까움) + 해외주식 원화환산 평가.
    - output2 가 비어 있으면: 종목 평가금(output1 합) + 해외 (기존과 동일).

    (output2 - 시드) / 시드 는 불가 — output2 는 dict 이므로, 위 스칼라 총자산으로
    (total_net_worth - seed_money) / seed_money * 100 을 쓴다.
    """
    from app.services.fx_rate import get_usd_krw_rate

    o = domestic_balance_output2 if isinstance(domestic_balance_output2, dict) else {}
    cash_deposit_krw = _kis_float(o, "dnca_tot_amt")
    domestic_nass = _kis_float(o, "nass_amt")
    domestic_tot_evlu = _kis_float(o, "tot_evlu_amt")
    domestic_scts_evlu = _kis_float(o, "scts_evlu_amt")

    kr = [h for h in holdings if h.get("currency", "KRW") != "USD"]
    usd = [h for h in holdings if h.get("currency") == "USD"]

    total_eval_krw = sum(h["eval_amount"] for h in kr)
    total_purchase_krw = sum(h["purchase_amount"] for h in kr)
    unrealized_pnl_krw = total_eval_krw - total_purchase_krw

    total_eval_usd = sum(h["eval_amount"] for h in usd)
    total_purchase_usd = sum(h["purchase_amount"] for h in usd)
    unrealized_pnl_usd = total_eval_usd - total_purchase_usd

    rate = get_usd_krw_rate()
    eval_usd_as_krw = (total_eval_usd * rate) if rate else None
    purchase_usd_as_krw = (total_purchase_usd * rate) if rate else None
    unreal_usd_as_krw = (unrealized_pnl_usd * rate) if rate else None

    if usd and not rate:
        logger.warning(
            "해외(USD) 보유가 있으나 환율을 가져오지 못해 원화 합계·수익률에서 해외 평가가 제외되었습니다. "
            "네트워크 확인 또는 FX_USD_KRW 환경변수로 수동 지정 가능."
        )

    current_eval_combined = total_eval_krw + (eval_usd_as_krw or 0.0)
    total_purchase_combined = total_purchase_krw + (purchase_usd_as_krw or 0.0)
    unrealized_combined = current_eval_combined - total_purchase_combined

    domestic_wealth_krw = 0.0
    seed_basis_includes_cash = False
    if domestic_nass > 0:
        domestic_wealth_krw = domestic_nass
        seed_basis_includes_cash = True
    elif domestic_tot_evlu > 0:
        domestic_wealth_krw = domestic_tot_evlu
        seed_basis_includes_cash = True

    if seed_basis_includes_cash:
        total_net_worth_krw = domestic_wealth_krw + (eval_usd_as_krw or 0.0)
    else:
        total_net_worth_krw = current_eval_combined

    total_return_pct = (
        ((total_net_worth_krw - seed_money) / seed_money * 100) if seed_money > 0 else 0.0
    )
    return_on_cost_pct = (
        (unrealized_combined / total_purchase_combined * 100)
        if total_purchase_combined > 0
        else 0.0
    )

    return {
        "seed_money": seed_money,
        "current_eval": current_eval_combined,
        "total_net_worth_krw": total_net_worth_krw,
        "current_eval_krw_domestic": total_eval_krw,
        "current_eval_usd": total_eval_usd,
        "current_eval_usd_as_krw": eval_usd_as_krw,
        "total_purchase": total_purchase_combined,
        "total_purchase_krw_domestic": total_purchase_krw,
        "purchase_amount_usd_total": total_purchase_usd,
        "purchase_amount_usd_as_krw": purchase_usd_as_krw,
        "unrealized_pnl": unrealized_combined,
        "unrealized_pnl_krw_domestic": unrealized_pnl_krw,
        "unrealized_pnl_usd": unrealized_pnl_usd,
        "unrealized_pnl_usd_as_krw": unreal_usd_as_krw,
        "cash_deposit_krw": cash_deposit_krw,
        "domestic_nass_krw": domestic_nass,
        "domestic_tot_evlu_krw": domestic_tot_evlu,
        "domestic_scts_evlu_krw": domestic_scts_evlu,
        "seed_basis_includes_cash": seed_basis_includes_cash,
        "pnl_vs_seed_krw": total_net_worth_krw - seed_money,
        "return_pct_on_seed": total_return_pct,
        "return_pct_on_holdings_cost": return_on_cost_pct,
        "return_on_cost_pct": return_on_cost_pct,
        "total_return_pct": total_return_pct,
        "estimated_balance": total_net_worth_krw,
        "holdings_count": len(holdings),
        "holdings_count_domestic": len(kr),
        "holdings_count_overseas": len(usd),
        "has_overseas": len(usd) > 0,
        "usd_krw_rate": rate,
        "fx_includes_usd": bool(rate and usd),
        "fx_usd_excluded_from_krw_totals": bool(usd and not rate),
    }
