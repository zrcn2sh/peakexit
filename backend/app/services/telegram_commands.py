"""
텔레그램 봇 명령어 처리

지원 명령어:
/start   - 시작 & 도움말
/help    - 도움말
/holdings - 보유종목 현황
/summary  - 시드·보유(매입) 기준 수익 요약
/overview — 요약+보유 한 번에 (수동 현황, 메시지 2통)
/현황     - /overview 와 동일
/atr      - 종목별 ATR 트레일링 설정
/status   - 시스템 상태
/check    - 즉시 매도 검사 실행
"""
import logging
import threading
import time
import requests
import os
from datetime import datetime

logger = logging.getLogger(__name__)

HELP_TEXT = """
🤖 <b>PeakExit 봇 명령어</b>
──────────────────────
/holdings  — 보유종목 + 수익률
/summary   — 시드·보유(매입) 기준 수익 요약
/overview  — 요약+보유 전체 (수동 현황, 메시지 2통)
/현황      — /overview 와 동일
/atr       — 종목별 트레일링 비율
/check     — 즉시 매도 검사 실행
/status    — 시스템 상태 확인
/help      — 이 도움말
──────────────────────
💡 매도 신호/결과는 자동으로 전송됩니다
""".strip()


class TelegramCommandHandler:
    def __init__(self, notifier):
        self.notifier = notifier
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.last_update_id = 0
        self._running = False
        self._thread = None

    def _get_updates(self) -> list:
        try:
            resp = requests.get(
                f"{self.base_url}/getUpdates",
                params={"offset": self.last_update_id + 1, "timeout": 30},
                timeout=35,
            )
            data = resp.json()
            return data.get("result", [])
        except Exception as e:
            logger.error(f"getUpdates 실패: {e}")
            return []

    def _send(self, text: str):
        self.notifier._send(text)

    def _build_portfolio_summary_message(self, summary: dict, seed: float) -> str:
        """포트폴리오 요약 본문 (/summary 와 동일 포맷)."""
        rate_seed = float(summary.get("return_pct_on_seed", summary["total_return_pct"]))
        rate_seed_str = f"+{rate_seed:.2f}%" if rate_seed >= 0 else f"{rate_seed:.2f}%"
        rate_seed_icon = "📈" if rate_seed >= 0 else "📉"
        cost_rate = float(
            summary.get("return_pct_on_holdings_cost", summary.get("return_on_cost_pct", 0.0))
        )
        cost_rate_str = f"+{cost_rate:.2f}%" if cost_rate >= 0 else f"{cost_rate:.2f}%"
        cost_rate_icon = "📈" if cost_rate >= 0 else "📉"
        pnl_seed = float(summary.get("pnl_vs_seed_krw", summary.get("total_net_worth_krw", summary["current_eval"]) - seed))
        pnl_seed_str = f"+{pnl_seed:,.0f}" if pnl_seed >= 0 else f"{pnl_seed:,.0f}"
        total_purchase = float(summary.get("total_purchase", 0.0))
        unreal = summary["unrealized_pnl"]
        unreal_str = f"+{unreal:,.0f}" if unreal >= 0 else f"{unreal:,.0f}"
        eval_usd = summary.get("current_eval_usd") or 0.0
        unreal_usd = summary.get("unrealized_pnl_usd") or 0.0
        fx_line = ""
        if summary.get("fx_includes_usd") and summary.get("usd_krw_rate"):
            fx_line = (
                f"\n💱 환율: 1 USD ≈ {summary['usd_krw_rate']:,.2f}원\n"
                f"🌎 해외 평가(USD {eval_usd:,.2f}) → 약 {summary.get('current_eval_usd_as_krw') or 0:,.0f}원\n"
                f"🌎 해외 미실현(USD {unreal_usd:+,.2f}) → 약 {(summary.get('unrealized_pnl_usd_as_krw') or 0):+,.0f}원"
            )
        elif summary.get("fx_usd_excluded_from_krw_totals"):
            fx_line = (
                f"\n⚠️ 해외 USD 평가 ${eval_usd:,.2f} — 환율 미조회로 원화 합계 제외\n"
                f"   (수동: .env 에 FX_USD_KRW=1450 형식)"
            )
        elif summary.get("has_overseas"):
            fx_line = f"\n🌎 해외(USD) 평가: ${eval_usd:,.2f} / 미실현 ${unreal_usd:+,.2f}"

        stocks_eval = float(summary["current_eval"])
        net_worth = float(summary.get("total_net_worth_krw", stocks_eval))
        cash_dep = float(summary.get("cash_deposit_krw") or 0.0)
        incl_cash = bool(summary.get("seed_basis_includes_cash"))

        acct_lines = f"주식 평가 합: {stocks_eval:,.0f}원{fx_line}\n"
        if cash_dep > 0:
            acct_lines += f"🏦 예수금(dnca_tot_amt): {cash_dep:,.0f}원\n"
        if incl_cash:
            acct_lines += f"📌 <b>총자산</b>(국내 output2 + 해외주식): {net_worth:,.0f}원\n"

        return (
            f"💼 <b>포트폴리오 요약</b>\n"
            f"{'─' * 22}\n"
            f"시드머니: {seed:,.0f}원\n"
            f"{acct_lines}"
            f"\n🌱 <b>시드머니 기준</b> (총자산 대비)\n"
            f"   대비 손익: {pnl_seed_str}원\n"
            f"   {rate_seed_icon} 수익률: <b>{rate_seed_str}</b>\n"
            f"\n📊 <b>보유(매입원가) 기준</b>\n"
            f"   총 매입원가: {total_purchase:,.0f}원\n"
            f"   평가손익: {unreal_str}원\n"
            f"   {cost_rate_icon} 수익률: <b>{cost_rate_str}</b>\n"
            f"\n추정 자산: {summary['estimated_balance']:,.0f}원\n"
            f"보유종목: {summary['holdings_count']}개 "
            f"(국내 {summary.get('holdings_count_domestic', 0)} / "
            f"해외 {summary.get('holdings_count_overseas', 0)})\n"
            f"🕐 {datetime.now().strftime('%m/%d %H:%M')}"
        )

    def _build_holdings_message(self, client, settings: dict, cache: dict, holdings: list) -> str:
        """보유종목 상세 본문 (/holdings 와 동일 포맷)."""
        if not holdings:
            return "📭 현재 보유 종목이 없습니다."

        from app.core.sell_engine import evaluate_sell, peak_tracker

        lines = []
        for h in holdings:
            client.enrich_holding_trading_meta(h, cache.get(h["ticker"]))
            ticker = h["ticker"]
            pr = h["profit_rate"]
            pr_str = f"+{pr:.2f}%" if pr >= 0 else f"{pr:.2f}%"
            pr_icon = "📈" if pr >= 0 else "📉"
            ccy = h.get("currency", "KRW")
            if ccy == "USD":
                px_line = f"현재 ${h['current_price']:,.2f} · 평균 ${h['avg_price']:,.2f}"
            else:
                px_line = f"현재가 {h['current_price']:,.0f}원"

            peak_info = peak_tracker.update(ticker, h["current_price"], h["avg_price"])
            drop = peak_info.get("drop_from_peak_pct", 0)
            drop_str = f"고점대비 {drop:.1f}%" if peak_info.get("peak_price") else ""

            tc = cache.get(ticker, {})
            trail = f"트레일링 {tc.get('trailing_drop_pct','?')}%" if tc else ""

            signal = evaluate_sell(h, settings, tc or None)
            signal_str = f"⚠️ {signal['reason_label']}" if signal else "✓ 보유중"
            tag = "[미국]" if ccy == "USD" else "[국내]"

            lines.append(
                f"{pr_icon} {tag} <b>{h['name']}</b> ({ticker})\n"
                f"   {px_line} | {pr_str}\n"
                f"   {drop_str}  {trail}\n"
                f"   {signal_str}"
            )

        return (
            f"📋 <b>보유종목 ({len(holdings)}개)</b>\n"
            f"{'─' * 22}\n"
            + "\n\n".join(lines)
        )

    # ── 명령어별 핸들러 ────────────────────────────
    def _cmd_help(self):
        self._send(HELP_TEXT)

    def _cmd_holdings(self):
        try:
            from app.services.kis_client import get_kis_client
            from app.services.trailing_advisor import _load_cache
            from app.core.state import get_settings

            client = get_kis_client()
            settings = get_settings()
            cache = _load_cache()
            holdings = client.get_holdings()
            self._send(self._build_holdings_message(client, settings, cache, holdings))
        except Exception as e:
            self._send(f"⚠️ 조회 실패: {e}")

    def _cmd_summary(self):
        try:
            from app.services.kis_client import get_kis_client
            from app.core.sell_engine import calc_portfolio_summary
            from app.core.state import get_settings

            client = get_kis_client()
            settings = get_settings()
            seed = settings.get("seed_money", 1_000_000)
            holdings = client.get_holdings()
            out2 = client.get_last_domestic_balance_output2()
            summary = calc_portfolio_summary(holdings, seed, [], out2)
            self._send(self._build_portfolio_summary_message(summary, seed))
        except Exception as e:
            self._send(f"⚠️ 조회 실패: {e}")

    def _cmd_overview(self):
        """요약 + 보유를 한 번에 (잔고 API 1회, 메시지 2통)."""
        try:
            from app.services.kis_client import get_kis_client
            from app.services.trailing_advisor import _load_cache
            from app.core.sell_engine import calc_portfolio_summary
            from app.core.state import get_settings

            client = get_kis_client()
            settings = get_settings()
            cache = _load_cache()
            seed = settings.get("seed_money", 1_000_000)
            holdings = client.get_holdings()
            out2 = client.get_last_domestic_balance_output2()
            summary = calc_portfolio_summary(holdings, seed, [], out2)

            self._send(
                "📊 <b>수동 현황</b> ①/② 요약\n\n"
                + self._build_portfolio_summary_message(summary, seed)
            )
            self._send(
                "📊 <b>수동 현황</b> ②/② 보유\n\n"
                + self._build_holdings_message(client, settings, cache, holdings)
            )
        except Exception as e:
            self._send(f"⚠️ 현황 조회 실패: {e}")

    def _cmd_atr(self):
        try:
            from app.services.trailing_advisor import _load_cache
            cache = _load_cache()
            if not cache:
                self._send("📭 ATR 데이터 없음\n보유종목 조회 시 자동 계산됩니다.")
                return

            lines = []
            for ticker, c in cache.items():
                cl = c.get("classification", {})
                conf = c.get("confidence", "?")
                conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(conf, "⚪")
                lines.append(
                    f"{conf_icon} <b>{ticker}</b> ({cl.get('size','?')})\n"
                    f"   ATR {c.get('atr_pct','?')}% → 트레일링 <b>{c.get('trailing_drop_pct','?')}%</b>\n"
                    f"   발동: {c.get('trailing_trigger_pct','?')}% | {c.get('reason','')[:40]}"
                )

            msg = (
                f"📊 <b>종목별 ATR 트레일링</b>\n"
                f"{'─' * 22}\n"
                + "\n\n".join(lines)
            )
            self._send(msg)
        except Exception as e:
            self._send(f"⚠️ 조회 실패: {e}")

    def _cmd_status(self):
        try:
            from app.core.state import get_settings
            settings = get_settings()
            enabled = "✅ 활성화" if settings.get("enabled", True) else "❌ 비활성화"
            mock = os.getenv("KIS_IS_MOCK", "false").lower() == "true"
            mock_str = "🟡 모의투자" if mock else "🟢 실전투자"

            msg = (
                f"⚙️ <b>시스템 상태</b>\n"
                f"{'─' * 22}\n"
                f"자동매도: {enabled}\n"
                f"투자모드: {mock_str}\n"
                f"손절기준: {settings.get('stop_loss_pct', -10)}%\n"
                f"체크주기: {settings.get('check_interval_minutes', 5)}분\n"
                f"시드머니: {settings.get('seed_money', 1_000_000):,.0f}원\n"
                f"🕐 {datetime.now().strftime('%m/%d %H:%M:%S')}"
            )
            self._send(msg)
        except Exception as e:
            self._send(f"⚠️ 상태 조회 실패: {e}")

    def _cmd_check(self):
        self._send("🔍 즉시 매도 검사를 실행합니다...")
        try:
            from app.core.scheduler import run_sell_check
            run_sell_check()
            self._send("✅ 매도 검사 완료")
        except Exception as e:
            self._send(f"⚠️ 검사 실패: {e}")

    # ── 메인 루프 ──────────────────────────────────
    def _process_update(self, update: dict):
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip().lower()

        # 등록된 chat_id만 허용
        if chat_id != str(self.chat_id):
            logger.warning(f"허용되지 않은 chat_id: {chat_id}")
            return

        cmd_map = {
            "/start":    self._cmd_help,
            "/help":     self._cmd_help,
            "/holdings": self._cmd_holdings,
            "/summary":  self._cmd_summary,
            "/overview": self._cmd_overview,
            "/현황":     self._cmd_overview,
            "/atr":      self._cmd_atr,
            "/status":   self._cmd_status,
            "/check":    self._cmd_check,
        }

        # @봇이름 suffix 제거
        cmd = text.split("@")[0]
        handler = cmd_map.get(cmd)
        if handler:
            logger.info(f"[텔레그램 명령] {cmd}")
            handler()
        elif text.startswith("/"):
            self._send(f"❓ 알 수 없는 명령어: {text}\n\n{HELP_TEXT}")

    def _poll_loop(self):
        logger.info("텔레그램 폴링 시작")
        while self._running:
            updates = self._get_updates()
            for update in updates:
                self.last_update_id = max(self.last_update_id, update["update_id"])
                try:
                    self._process_update(update)
                except Exception as e:
                    logger.error(f"명령어 처리 실패: {e}")
            if not updates:
                time.sleep(1)

    def start(self):
        if not self.token or not self.chat_id:
            logger.warning("텔레그램 미설정 — 봇 명령어 비활성화")
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("텔레그램 봇 명령어 핸들러 시작")

    def stop(self):
        self._running = False


_handler = None

def start_command_handler(notifier):
    global _handler
    _handler = TelegramCommandHandler(notifier)
    _handler.start()
    return _handler
