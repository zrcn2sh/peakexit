"""
텔레그램 알림 서비스

발송 시점:
1. 매도 신호 감지 (실행 전 예고)
2. 매도 실행 결과 (성공/실패)
3. 수익률 일간 리포트 (장 시작 전 08:55, 장 마감 후 15:35)
4. AI 트레일링 비율 갱신 결과 (주 1회)
"""
import os
import logging
import requests
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

logger = logging.getLogger(__name__)
SEOUL_TZ = ZoneInfo("Asia/Seoul")


def _holding_currency(h: dict) -> str:
    return h.get("currency") or ("USD" if h.get("ovrs_excg_cd") else "KRW")


def _fmt_money_text(currency: str, amount: float, *, decimals: int = 2) -> str:
    if currency == "USD":
        return f"${amount:,.{decimals}f}"
    return f"{amount:,.0f}원"


def _signal_currency(signal: dict) -> str:
    return signal.get("currency") or ("USD" if signal.get("ovrs_excg_cd") else "KRW")


def format_pending_sell_compact(summary: dict) -> str:
    """매도 미결제·차감 — 한 줄 요약."""
    pending = float(
        summary.get("pending_sell_settlement_krw")
        or summary.get("pending_cash_adjustment_krw")
        or summary.get("pending_sell_proceeds_krw")
        or 0
    )
    dom_p = float(summary.get("pending_sell_settlement_dom_krw") or 0)
    ov_p = float(summary.get("pending_sell_settlement_ov_krw") or 0)
    deduct = float(summary.get("deductions_krw") or 0)
    if pending <= 0 and deduct <= 0:
        return ""
    lines: list[str] = []
    if pending > 0:
        detail = f"국내 T+2 {dom_p:,.0f} + 해외 T+1 {ov_p:,.0f}".strip()
        lines.append(f"   └ 미결제 매도 <b>+{pending:,.0f}원</b> ({detail})\n")
    if deduct > 0:
        nrcvb = float(summary.get("nrcvb_buy_amt_krw") or 0)
        loan = float(summary.get("credit_loan_krw") or 0)
        lines.append(
            f"   └ 차감 <b>−{deduct:,.0f}원</b> (미수 {nrcvb:,.0f} + 대출 {loan:,.0f})\n"
        )
    return "".join(lines)


def format_pending_sell_adjustment_html(summary: dict) -> str:
    """하위 호환 — 간결 포맷."""
    return format_pending_sell_compact(summary)


def _pct_str(rate: float) -> str:
    return f"+{rate:.2f}%" if rate >= 0 else f"{rate:.2f}%"


def _pnl_krw_str(amount: float) -> str:
    return f"+{amount:,.0f}" if amount >= 0 else f"{amount:,.0f}"


def format_portfolio_summary_html(
    summary: dict,
    seed: float,
    *,
    title: str = "포트폴리오 요약",
    show_holdings_count: bool = True,
) -> str:
    """총자산·시드·매입원가 핵심만."""
    net = float(
        summary.get("total_net_worth_krw")
        or summary.get("estimated_balance")
        or summary.get("current_eval")
        or 0
    )
    rate_seed = float(summary.get("return_pct_on_seed", summary.get("total_return_pct", 0)))
    cost_rate = float(
        summary.get("return_pct_on_holdings_cost", summary.get("return_on_cost_pct", 0))
    )
    pnl_seed = float(summary.get("pnl_vs_seed_krw", net - seed))
    total_purchase = float(summary.get("total_purchase", 0))
    unreal = float(summary.get("unrealized_pnl", 0))
    pending = format_pending_sell_compact(summary)

    seed_icon = "📈" if rate_seed >= 0 else "📉"
    cost_icon = "📈" if cost_rate >= 0 else "📉"

    lines = [
        f"💼 <b>{title}</b>\n",
        f"{'─' * 18}\n",
        f"💰 <b>추정 총자산</b> {net:,.0f}원\n",
    ]
    if pending:
        lines.append(pending)

    lines.extend([
        f"\n🌱 시드 {seed:,.0f}원 → {_pnl_krw_str(pnl_seed)}원 "
        f"({seed_icon} <b>{_pct_str(rate_seed)}</b>)\n",
        f"📊 매입 {total_purchase:,.0f}원 · 평가손익 {_pnl_krw_str(unreal)}원 "
        f"({cost_icon} <b>{_pct_str(cost_rate)}</b>)\n",
    ])

    if show_holdings_count:
        n = int(summary.get("holdings_count", 0))
        nd = int(summary.get("holdings_count_domestic", 0))
        no = int(summary.get("holdings_count_overseas", 0))
        lines.append(f"\n📋 보유 {n}종목 (국내 {nd} · 해외 {no})\n")

    lines.append(f"🕐 {datetime.now(SEOUL_TZ).strftime('%m/%d %H:%M')}")
    return "".join(lines)


def _holding_eval_brief(h: dict, fx_rate: float = 0) -> str:
    """보유금액 한 줄 (USD는 원화 환산)."""
    qty = int(h.get("quantity") or 0)
    price = float(h.get("current_price") or 0)
    ccy = _holding_currency(h)
    if ccy == "USD":
        usd = float(h.get("eval_amount_usd") or h.get("eval_amount") or 0)
        if usd <= 0 and qty > 0 and price > 0:
            usd = qty * price
        krw = float(h.get("eval_amount_krw") or 0)
        fx = float(h.get("fx_rate") or fx_rate or 0)
        if krw <= 0 and usd > 0 and fx > 0:
            krw = round(usd * fx)
        if usd > 0 and krw > 0:
            return f"{_fmt_money_text('USD', usd, decimals=0)} (≈{krw:,.0f}원)"
        if usd > 0:
            return _fmt_money_text("USD", usd, decimals=0)
        return ""
    krw = float(h.get("eval_amount") or 0)
    if krw <= 0 and qty > 0 and price > 0:
        krw = qty * price
    return f"{krw:,.0f}원" if krw > 0 else ""


def format_holdings_list_html(
    holdings: list[dict],
    *,
    fx_rate: float = 0,
    max_rows: int = 15,
    signal_only_extra: bool = False,
) -> str:
    """종목별 수익률·보유금액·신호 (간결)."""
    if not holdings:
        return "📭 보유 종목 없음"

    def _safe_pr(h: dict) -> float:
        try:
            v = float(h.get("profit_rate", 0) or 0)
            return v if math.isfinite(v) else 0.0
        except Exception:
            return 0.0

    rows = sorted(holdings, key=_safe_pr, reverse=True)
    lines = [f"📋 <b>보유 {len(holdings)}종목</b>", f"{'─' * 18}"]

    for h in rows[:max_rows]:
        pr = _safe_pr(h)
        icon = "📈" if pr >= 0 else "📉"
        tag = "US" if _holding_currency(h) == "USD" else "KR"
        name = (h.get("name") or h.get("ticker", ""))[:10]
        ticker = h.get("ticker", "")
        eval_brief = _holding_eval_brief(h, fx_rate)
        eval_part = f" · {eval_brief}" if eval_brief else ""

        sig = ""
        if h.get("should_sell") and h.get("sell_signal"):
            sig = f" · ⚠️ {h['sell_signal'].get('reason_label', '매도')[:8]}"
        elif not signal_only_extra and h.get("sell_signal"):
            sig = ""

        lines.append(
            f"{icon} [{tag}] <b>{name}</b> {_pct_str(pr)}{eval_part}{sig}"
        )

    if len(rows) > max_rows:
        lines.append(f"… 외 {len(rows) - max_rows}종목")

    return "\n".join(lines)


def _sell_result_amount_lines(result: dict) -> str:
    """매도 결과 — 핵심 금액만."""
    ccy = _signal_currency(result)
    qty = int(result.get("quantity") or 0)
    sell = float(result.get("sell_price") or result.get("current_price") or 0)
    sell_total = result.get("sell_total")
    profit_amount = result.get("profit_amount")
    profit_krw = result.get("profit_amount_krw")

    if sell_total is None and sell and qty:
        sell_total = sell * qty
    avg = float(result.get("avg_price") or 0)
    if profit_amount is None and avg and sell and qty:
        profit_amount = sell * qty - avg * qty

    pnl = float(result.get("profit_rate", 0) or 0)
    dec = 2 if ccy == "USD" else 0
    profit_amt = float(profit_amount or 0)
    if ccy == "USD":
        profit_str = f"${profit_amt:+,.2f}"
    else:
        profit_str = f"{profit_amt:+,.0f}원"

    line1 = (
        f"🔢 {qty:,}주 · 매도 {_fmt_money_text(ccy, sell, decimals=dec)}"
        f" → {_fmt_money_text(ccy, float(sell_total or 0), decimals=dec)}"
    )
    line2 = f"📈 <b>{_pct_str(pnl)}</b> · 손익 {profit_str}"
    if ccy == "USD" and profit_krw is not None:
        line2 += f" (≈{float(profit_krw):+,.0f}원)"
    return f"{line1}\n{line2}"


def _market_status_line() -> str:
    """한국/미국 시장 상태를 동시에 표시."""
    now = datetime.now(SEOUL_TZ)
    wd = now.weekday()  # mon=0 ... sun=6
    hhmm = now.hour * 100 + now.minute

    kr_open = wd < 5 and 900 <= hhmm <= 1530
    # 미국장(KST): 23:30~24:00 (월~금) + 00:00~06:00 (화~토)
    us_open = (wd < 5 and hhmm >= 2330) or (1 <= wd <= 5 and hhmm <= 600)

    kr = "장중" if kr_open else "장외"
    us = "장중" if us_open else "장외"
    return f"🕒 한국장: {kr} · 미국장: {us}"


class TelegramNotifier:
    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            logger.warning("텔레그램 설정 없음 (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 미설정)")

    def _send(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self.enabled:
            logger.debug(f"[텔레그램 비활성] {text[:60]}...")
            return False
        try:
            resp = requests.post(
                self.BASE_URL.format(token=self.token),
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"텔레그램 발송 실패: {e}")
            return False

    # ─────────────────────────────────────────────────
    # 1. 매도 신호 감지 예고
    # ─────────────────────────────────────────────────
    def notify_sell_signal(self, signal: dict):
        reason_emoji = {
            "STOP_LOSS":     "🔴",
            "TRAILING_STOP": "🟡",
            "TAKE_PROFIT":   "🟢",
            "MANUAL":        "🔵",
        }.get(signal.get("reason", ""), "⚪")

        pnl = signal.get("profit_rate", 0)
        pnl_str = f"+{pnl:.2f}%" if pnl >= 0 else f"{pnl:.2f}%"

        ccy = _signal_currency(signal)
        price_str = _fmt_money_text(ccy, float(signal.get("current_price", 0)))

        trailing_info = ""
        if signal.get("reason") == "TRAILING_STOP":
            trailing_info = (
                f"\n📉 고점대비 {signal.get('drop_from_peak_pct', 0):.1f}%"
            )

        msg = (
            f"{reason_emoji} <b>매도 신호</b> {signal.get('reason_label', '')}\n"
            f"<b>{signal.get('name', '')}</b> ({signal.get('ticker', '')})\n"
            f"💰 {price_str} · <b>{pnl_str}</b>{trailing_info}\n"
            f"🕐 {datetime.now(SEOUL_TZ).strftime('%H:%M')}"
        )
        self._send(msg)

    # ─────────────────────────────────────────────────
    # 2. 매도 실행 결과
    # ─────────────────────────────────────────────────
    def notify_sell_result(self, result: dict):
        success = result.get("success", False)
        icon = "✅" if success else "❌"
        status = "매도 완료" if success else "매도 실패"

        order_info = f"\n🧾 주문번호: {result.get('order_no', 'N/A')}" if success else \
                     f"\n⚠️ 오류: {result.get('error', '')}"

        amount_block = _sell_result_amount_lines(result)

        msg = (
            f"{icon} <b>{status}</b> · {result.get('reason_label', result.get('reason', ''))}\n"
            f"<b>{result.get('name', '')}</b> ({result.get('ticker', '')})\n"
            f"{amount_block}"
            f"{order_info}\n"
            f"🕐 {datetime.now(SEOUL_TZ).strftime('%H:%M')}"
        )
        self._send(msg)

    # ─────────────────────────────────────────────────
    # 3. 수익률 일간 리포트
    # ─────────────────────────────────────────────────
    def notify_daily_report(self, summary: dict, holdings: list[dict], report_type: str = "close"):
        if isinstance(report_type, str) and report_type.startswith("hourly_"):
            market = report_type.split("_", 1)[1].upper() if "_" in report_type else "KR"
            icon = "⏱️"
            title = f"{market} 장중 체크"
        else:
            icon = "🌅" if report_type == "open" else "🌇"
            title = "장 시작" if report_type == "open" else "장 마감"

        seed = float(summary.get("seed_money", 0))
        fx = float(summary.get("usd_krw_rate") or 0)
        body = format_portfolio_summary_html(
            summary, seed, title=f"{icon} {title}", show_holdings_count=False
        )
        holdings_block = format_holdings_list_html(holdings, fx_rate=fx, max_rows=12)

        signals = [h for h in holdings if h.get("should_sell")]
        signal_text = ""
        if signals:
            parts = []
            for s in signals[:5]:
                lbl = (s.get("sell_signal") or {}).get("reason_label", "매도")
                parts.append(f"{(s.get('name') or s.get('ticker', ''))[:8]}({lbl[:6]})")
            extra = f" 외 {len(signals) - 5}건" if len(signals) > 5 else ""
            signal_text = f"\n\n⚠️ <b>매도신호</b> " + ", ".join(parts) + extra

        msg = (
            f"{body}\n"
            f"\n{_market_status_line()}\n\n"
            f"{holdings_block}"
            f"{signal_text}"
        )
        self._send(msg)

    # ─────────────────────────────────────────────────
    # 4. AI 트레일링 갱신 결과
    # ─────────────────────────────────────────────────
    def notify_trailing_update(self, configs: dict):
        if not configs:
            return

        lines = []
        for ticker, cfg in configs.items():
            cl = cfg.get("classification", {})
            size = cl.get("size", "?")
            drop = cfg.get("trailing_drop_pct", "?")
            atr = cfg.get("atr_pct", "?")
            conf = cfg.get("confidence", "?")
            conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(conf, "⚪")
            lines.append(f"  {conf_icon} {ticker} ({size}) → <b>{drop}%</b> [ATR {atr}%]")

        holdings_text = "\n".join(lines[:12])
        extra = f"\n… 외 {len(lines) - 12}종목" if len(lines) > 12 else ""
        msg = (
            f"🤖 <b>트레일링 갱신</b>\n"
            f"{'─' * 18}\n"
            f"{holdings_text}{extra}\n"
            f"🕐 {datetime.now(SEOUL_TZ).strftime('%m/%d %H:%M')}"
        )
        self._send(msg)

    # ─────────────────────────────────────────────────
    # 시스템 알림
    # ─────────────────────────────────────────────────
    def notify_system(self, message: str, level: str = "info"):
        icon = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}.get(level, "ℹ️")
        self._send(f"{icon} <b>PeakExit 시스템</b>\n{message}")


# 싱글톤
_notifier: Optional[TelegramNotifier] = None

def get_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
