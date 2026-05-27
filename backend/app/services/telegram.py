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
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def _holding_currency(h: dict) -> str:
    return h.get("currency") or ("USD" if h.get("ovrs_excg_cd") else "KRW")


def _fmt_money_text(currency: str, amount: float, *, decimals: int = 2) -> str:
    if currency == "USD":
        return f"${amount:,.{decimals}f}"
    return f"{amount:,.0f}원"


def _signal_currency(signal: dict) -> str:
    return signal.get("currency") or ("USD" if signal.get("ovrs_excg_cd") else "KRW")


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

        trailing_info = ""
        if signal.get("reason") == "TRAILING_STOP":
            trailing_info = (
                f"\n📌 고점: {signal.get('peak_price', 0):,.0f}원 "
                f"(+{signal.get('peak_profit_rate', 0):.1f}%)"
                f"\n📉 고점 대비: {signal.get('drop_from_peak_pct', 0):.1f}%"
                f"\n⚙️ 적용 기준: {signal.get('trailing_source', '')}"
            )

        ccy = _signal_currency(signal)
        price_str = _fmt_money_text(ccy, float(signal.get("current_price", 0)))

        msg = (
            f"{reason_emoji} <b>매도 신호 감지</b>\n"
            f"{'─' * 22}\n"
            f"📊 <b>{signal.get('name', '')} ({signal.get('ticker', '')})</b>\n"
            f"📋 사유: {signal.get('reason_label', '')}\n"
            f"💰 현재가: {price_str}\n"
            f"📈 수익률: <b>{pnl_str}</b>"
            f"{trailing_info}\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )
        self._send(msg)

    # ─────────────────────────────────────────────────
    # 2. 매도 실행 결과
    # ─────────────────────────────────────────────────
    def notify_sell_result(self, result: dict):
        success = result.get("success", False)
        icon = "✅" if success else "❌"
        status = "매도 완료" if success else "매도 실패"

        pnl = result.get("profit_rate", 0)
        pnl_str = f"+{pnl:.2f}%" if pnl >= 0 else f"{pnl:.2f}%"
        pnl_icon = "📈" if pnl >= 0 else "📉"

        order_info = f"\n🧾 주문번호: {result.get('order_no', 'N/A')}" if success else \
                     f"\n⚠️ 오류: {result.get('error', '')}"

        ccy = _signal_currency(result)
        price_str = _fmt_money_text(ccy, float(result.get("current_price", 0)))

        msg = (
            f"{icon} <b>{status}</b>\n"
            f"{'─' * 22}\n"
            f"📊 <b>{result.get('name', '')} ({result.get('ticker', '')})</b>\n"
            f"📋 사유: {result.get('reason_label', result.get('reason', ''))}\n"
            f"🔢 수량: {result.get('quantity', 0):,}주\n"
            f"💰 기준가: {price_str}\n"
            f"{pnl_icon} 수익률: <b>{pnl_str}</b>"
            f"{order_info}\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )
        self._send(msg)

    # ─────────────────────────────────────────────────
    # 3. 수익률 일간 리포트
    # ─────────────────────────────────────────────────
    def notify_daily_report(self, summary: dict, holdings: list[dict], report_type: str = "close"):
        icon = "🌅" if report_type == "open" else "🌇"
        title = "장 시작 전 현황" if report_type == "open" else "장 마감 후 결산"

        seed = summary.get("seed_money", 0)
        stocks_eval = float(summary.get("current_eval", 0))
        eval_amt = float(summary.get("total_net_worth_krw", summary.get("current_eval", 0)))
        eval_usd = summary.get("current_eval_usd") or 0.0
        unrealized = summary.get("unrealized_pnl", 0)
        unreal_usd = summary.get("unrealized_pnl_usd") or 0.0
        total_rate = float(summary.get("return_pct_on_seed", summary.get("total_return_pct", 0)))
        cost_rate = float(
            summary.get("return_pct_on_holdings_cost", summary.get("return_on_cost_pct", 0.0))
        )
        balance = summary.get("estimated_balance", 0)
        pnl_seed = float(summary.get("pnl_vs_seed_krw", eval_amt - seed))
        total_purchase = float(summary.get("total_purchase", 0.0))

        rate_icon = "📈" if total_rate >= 0 else "📉"
        rate_str = f"+{total_rate:.2f}%" if total_rate >= 0 else f"{total_rate:.2f}%"
        cost_icon = "📈" if cost_rate >= 0 else "📉"
        cost_str = f"+{cost_rate:.2f}%" if cost_rate >= 0 else f"{cost_rate:.2f}%"
        pnl_seed_str = f"+{pnl_seed:,.0f}" if pnl_seed >= 0 else f"{pnl_seed:,.0f}"
        unreal_str = f"+{unrealized:,.0f}" if unrealized >= 0 else f"{unrealized:,.0f}"
        eval_note = ""
        if summary.get("fx_includes_usd") and summary.get("usd_krw_rate"):
            usd_krw = summary.get("current_eval_usd_as_krw") or 0.0
            eval_note = (
                f"\n   · 1 USD ≈ {summary['usd_krw_rate']:,.2f}원\n"
                f"   · 해외 평가(USD {eval_usd:,.2f}) → 약 {usd_krw:,.0f}원"
            )
        elif summary.get("fx_usd_excluded_from_krw_totals"):
            eval_note = "\n⚠️ 해외(USD)는 환율 미조회로 원화 합계에서 제외"

        unreal_note = ""
        if summary.get("fx_includes_usd") and summary.get("has_overseas"):
            uak = summary.get("unrealized_pnl_usd_as_krw")
            if uak is not None:
                unreal_note = f"\n   · 해외 미실현(원화): {(uak):+,.0f}원 (USD {_fmt_money_text('USD', unreal_usd)})"

        # 보유종목 상위 5개 (수익률 순)
        top_holdings = sorted(holdings, key=lambda h: h.get("profit_rate", 0), reverse=True)[:5]
        holdings_text = ""
        for h in top_holdings:
            pr = h.get("profit_rate", 0)
            pr_str = f"+{pr:.1f}%" if pr >= 0 else f"{pr:.1f}%"
            ccy = _holding_currency(h)
            mkt = "[미국]" if ccy == "USD" else "[국내]"
            peak_drop = ""
            if h.get("peak_info", {}).get("drop_from_peak_pct"):
                d = h["peak_info"]["drop_from_peak_pct"]
                peak_drop = f" (고점대비 {d:.1f}%)"
            trailing_pct = ""
            if h.get("trailing_config", {}).get("trailing_drop_pct"):
                trailing_pct = f" | 트레일링 {h['trailing_config']['trailing_drop_pct']}%"
            holdings_text += f"\n  {mkt} {h.get('name','')[:8]} {pr_str}{peak_drop}{trailing_pct}"

        # 매도신호 있는 종목
        signal_text = ""
        signals = [h for h in holdings if h.get("should_sell")]
        if signals:
            signal_text = f"\n\n⚠️ <b>매도 신호 {len(signals)}건</b>"
            for s in signals:
                signal_text += f"\n  🔔 {s.get('name','')} — {s.get('sell_signal',{}).get('reason_label','')}"

        cash_dep = float(summary.get("cash_deposit_krw") or 0.0)
        incl_cash = bool(summary.get("seed_basis_includes_cash"))
        acct_extra = ""
        if incl_cash:
            acct_extra = f"\n   · 주식 평가 합: {stocks_eval:,.0f}원"
            if cash_dep > 0:
                acct_extra += f" · 예수금(dnca): {cash_dep:,.0f}원"

        msg = (
            f"{icon} <b>{title}</b>  {datetime.now().strftime('%m/%d')}\n"
            f"{'─' * 22}\n"
            f"💼 시드머니: {seed:,.0f}원\n"
            f"📦 총자산(시드 기준): {eval_amt:,.0f}원{eval_note}{acct_extra}\n"
            f"\n🌱 <b>시드머니 기준</b> 손익 {pnl_seed_str}원 · "
            f"{rate_icon} 수익률 <b>{rate_str}</b>\n"
            f"📊 <b>보유(매입원가)</b> 매입 {total_purchase:,.0f}원 · "
            f"평가손익 {unreal_str}원{unreal_note}\n"
            f"   {cost_icon} 매입 대비 수익률 <b>{cost_str}</b>\n"
            f"🏦 추정 자산(평가합): {balance:,.0f}원\n"
            f"\n📋 <b>보유종목 ({summary.get('holdings_count',0)}개)</b>"
            f"{holdings_text}"
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

        holdings_text = "\n".join(lines)
        msg = (
            f"🤖 <b>AI 트레일링 비율 갱신</b>\n"
            f"{'─' * 22}\n"
            f"{holdings_text}\n"
            f"\n🕐 {datetime.now().strftime('%m/%d %H:%M')}"
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
