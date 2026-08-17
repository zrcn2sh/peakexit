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
/debug    - 국내/해외 잔고 API raw (총자산 디버깅)
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
/holdings  — 보유종목 (수익률·보유금액)
/summary   — 총자산·시드·매입원가 요약
/overview  — 요약 + 보유 (2통)
/현황      — /overview 와 동일
/atr       — 종목별 트레일링 비율
/check     — 즉시 매도 검사 실행
/status    — 시스템 상태 확인
/debug     — 잔고 API raw (총자산 디버깅)
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
        from app.services.telegram import format_portfolio_summary_html

        return format_portfolio_summary_html(summary, seed)

    def _build_holdings_message(
        self,
        client,
        settings: dict,
        cache: dict,
        holdings: list,
        *,
        fx_rate: float = 0,
    ) -> str:
        """보유종목 (/holdings)."""
        if not holdings:
            return "📭 현재 보유 종목이 없습니다."

        from app.core.sell_engine import evaluate_sell, peak_tracker
        from app.services.telegram import format_holdings_list_html

        if not fx_rate:
            try:
                from app.services.fx_rate import get_usd_krw_rate
                fx_rate = float(get_usd_krw_rate() or 0)
            except Exception:
                fx_rate = 0

        enriched = []
        for h in holdings:
            client.enrich_holding_trading_meta(h, cache.get(h["ticker"]))
            ticker = h["ticker"]
            peak_tracker.update(ticker, h["current_price"], h["avg_price"])
            tc = cache.get(ticker, {})
            signal = evaluate_sell(h, settings, tc or None)
            enriched.append({
                **h,
                "should_sell": signal is not None,
                "sell_signal": signal,
            })

        return format_holdings_list_html(enriched, fx_rate=fx_rate, max_rows=20)

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

            fx = float(summary.get("usd_krw_rate") or 0)
            self._send(self._build_portfolio_summary_message(summary, seed))
            self._send(
                self._build_holdings_message(
                    client, settings, cache, holdings, fx_rate=fx
                )
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
                conf = c.get("confidence", "?")
                conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(conf, "⚪")
                lines.append(
                    f"{conf_icon} <b>{ticker}</b> "
                    f"트레일링 <b>{c.get('trailing_drop_pct', '?')}%</b> "
                    f"(ATR {c.get('atr_pct', '?')}%)"
                )

            msg = (
                f"📊 <b>ATR 트레일링</b>\n"
                f"{'─' * 18}\n"
                + "\n".join(lines)
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

    def _cmd_debug(self):
        """국내/해외 inquire-balance raw → 로그 + 텔레그램."""
        self._send("🔧 잔고 API raw 조회 중… (국내 TTTC8434R, 해외 TTTS3012R NASD/NYSE/AMEX)")
        try:
            from app.services.kis_client import get_kis_client
            from app.services.balance_debug import (
                fetch_balance_debug_snapshot,
                log_balance_debug_snapshot,
                telegram_messages_from_snapshot,
            )

            client = get_kis_client()
            snapshot = fetch_balance_debug_snapshot(client)
            log_balance_debug_snapshot(snapshot)
            messages = telegram_messages_from_snapshot(snapshot)
            for msg in messages:
                self._send(msg)
            if not messages:
                self._send("⚠️ 디버그 출력이 비어 있습니다.")
        except Exception as e:
            logger.exception("balance /debug 실패")
            self._send(f"⚠️ /debug 실패: {e}")

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
            "/debug":    self._cmd_debug,
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
