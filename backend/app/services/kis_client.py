"""
한국투자증권 API 클라이언트
실전/모의 투자 모두 지원
"""
import os
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from typing import Optional
import logging

from app.core.account_valuation import (
    OverseasValuation,
    overseas_from_inquire_balance_nasd,
    overseas_from_present_balance,
)
from app.core.portfolio_adjustment import parse_ccld_datetime_kst

logger = logging.getLogger(__name__)


class KISBusinessError(Exception):
    """HTTP 200이어도 rt_cd != 0 인 한투 API 본문 오류."""

    def __init__(self, message: str, payload: Optional[dict] = None):
        super().__init__(message)
        self.payload = payload or {}


def _kiss_check_rt_cd(data: dict) -> dict:
    if not isinstance(data, dict):
        raise KISBusinessError("응답이 JSON 객체가 아닙니다", {})
    rt = data.get("rt_cd")
    if rt is None:
        return data
    if str(rt) != "0":
        raise KISBusinessError(
            data.get("msg1") or f"API 오류 rt_cd={rt}",
            data,
        )
    return data


def market_to_quote_excd(market: str) -> str:
    """일봉/현재가 조회용 EXCD (NAS, NYS, AMS)."""
    m = (market or "").strip().upper()
    mp = {
        "NASDAQ": "NAS",
        "NYSE": "NYS",
        "AMEX": "AMS",
        "NAS": "NAS",
        "NYS": "NYS",
        "AMS": "AMS",
        "NASD": "NAS",
    }
    return mp.get(m, "NAS")


def market_to_order_excd(market: str) -> str:
    """주문용 OVRS_EXCG_CD (NASD, NYSE, AMEX)."""
    m = (market or "").strip().upper()
    mp = {
        "NASDAQ": "NASD",
        "NYSE": "NYSE",
        "AMEX": "AMEX",
        "NAS": "NASD",
        "NYS": "NYSE",
        "AMS": "AMEX",
        "NASD": "NASD",
    }
    return mp.get(m, "NASD")


_ORDER_EXCD_TO_QUOTE = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}

_OVRS_MARKET_LABEL = {"NASD": "NASDAQ", "NYSE": "NYSE", "AMEX": "AMEX"}


def _kis_float(item: dict, *keys: str, default: float = 0.0) -> float:
    for k in keys:
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            try:
                return float(str(v).replace(",", ""))
            except ValueError:
                continue
    return default


def _kis_int(item: dict, *keys: str, default: int = 0) -> int:
    return int(_kis_float(item, *keys, default=float(default)))


def _overseas_balance_row_key(row: dict) -> str:
    t = (row.get("ticker") or "").strip().upper()
    x = (row.get("ovrs_excg_cd") or "NASD").strip().upper()
    return f"{t}:{x}"


class KISApiClient:
    """한국투자증권 Open API 클라이언트"""

    REAL_BASE = "https://openapi.koreainvestment.com:9443"
    MOCK_BASE = "https://openapivts.koreainvestment.com:29443"

    def __init__(self):
        self.app_key = os.getenv("KIS_APP_KEY", "")
        self.app_secret = os.getenv("KIS_APP_SECRET", "")
        self.account_no = os.getenv("KIS_ACCOUNT_NO", "")      # 예: "12345678-01"
        self.is_mock = os.getenv("KIS_IS_MOCK", "false").lower() == "true"
        self.base_url = self.MOCK_BASE if self.is_mock else self.REAL_BASE

        self._access_token: Optional[str] = None
        self._token_expires: Optional[datetime] = None
        self._us_exch_cache: dict[str, tuple[str, str, float]] = {}
        # 국내 잔고조회(TTTC8434R) output2 — 예수금·총평가 등 (마지막 응답 기준)
        self._last_domestic_output2: dict = {}
        # 해외 체결기준(CTRP6504R) / inquire-balance NASD 폴백 — 원화 총평가
        self._last_overseas_eval_krw: float = 0.0
        self._last_overseas_eval_detail: dict = {}
        self._last_overseas_present_raw: dict = {}
        self._last_overseas_valuation: OverseasValuation = OverseasValuation.empty()

    # ─────────────────────────────────────────
    # 인증
    # ─────────────────────────────────────────
    def get_access_token(self) -> str:
        if self._access_token and self._token_expires and datetime.now() < self._token_expires:
            return self._access_token

        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expires = datetime.now() + timedelta(hours=23)
        logger.info("KIS 액세스 토큰 발급 완료")
        return self._access_token

    def _headers(self, tr_id: str, extra: dict = None) -> dict:
        h = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {self.get_access_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }
        if extra:
            h.update(extra)
        return h

    def _split_account(self) -> tuple[str, str]:
        parts = self.account_no.split("-", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError("KIS_ACCOUNT_NO는 '12345678-01' 형식이어야 합니다.")
        return parts[0], parts[1]

    # ─────────────────────────────────────────
    # 잔고 조회
    # ─────────────────────────────────────────
    def _row_from_domestic_item(self, item: dict) -> Optional[dict]:
        # 당일 매수는 hldg_qty=0·ord_psbl_qty>0 인 경우가 있음
        qty = _kis_int(
            item,
            "hldg_qty",
            "ord_psbl_qty",
            "thdt_buyqty",
            "thdt_buy_qty",
            "bfdy_buy_qty",
        )
        if qty <= 0:
            return None
        ticker = (item.get("pdno") or "").strip()
        if not ticker:
            return None
        avg_price = _kis_float(item, "pchs_avg_pric")
        current_price = _kis_float(item, "prpr")
        eval_amount = _kis_float(item, "evlu_amt")
        purchase_amount = _kis_float(item, "pchs_amt")
        if eval_amount <= 0 and qty > 0 and current_price > 0:
            eval_amount = qty * current_price
        if purchase_amount <= 0 and qty > 0 and avg_price > 0:
            purchase_amount = qty * avg_price
        profit_rate = _kis_float(item, "evlu_pfls_rt")
        if profit_rate == 0 and purchase_amount > 0:
            profit_rate = (eval_amount - purchase_amount) / purchase_amount * 100
        return {
            "ticker": ticker,
            "name": item.get("prdt_name") or ticker,
            "quantity": qty,
            "avg_price": avg_price,
            "current_price": current_price,
            "profit_rate": profit_rate,
            "eval_amount": eval_amount,
            "purchase_amount": purchase_amount,
            "currency": "KRW",
        }

    def fetch_domestic_inquire_balance_raw(self) -> dict:
        """국내 inquire-balance(TTTC8434R) raw — output1 전체·output2 (페이징). /debug용."""
        acct, suffix = self._split_account()
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        tr_id = "VTTC8434R" if self.is_mock else "TTTC8434R"
        ctx_fk, ctx_nk = "", ""
        all_output1: list[dict] = []
        last_output2: dict = {}
        last_data: dict = {}

        for page in range(20):
            params = {
                "CANO": acct,
                "ACNT_PRDT_CD": suffix,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": ctx_fk,
                "CTX_AREA_NK100": ctx_nk,
            }
            resp = requests.get(url, headers=self._headers(tr_id), params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            last_data = data
            if str(data.get("rt_cd", "0")) != "0":
                return {
                    "tr_id": tr_id,
                    "rt_cd": data.get("rt_cd"),
                    "msg1": data.get("msg1"),
                    "output1": all_output1,
                    "output2": last_output2,
                    "pages": page + 1,
                    "error": data.get("msg1"),
                }

            for item in data.get("output1") or []:
                if isinstance(item, dict):
                    all_output1.append(dict(item))

            out2 = data.get("output2")
            if isinstance(out2, list) and out2:
                out2 = out2[0] if isinstance(out2[0], dict) else {}
            if isinstance(out2, dict) and out2:
                last_output2 = dict(out2)
                self._last_domestic_output2 = last_output2

            tr_cont = (resp.headers.get("tr_cont") or data.get("tr_cont") or "").strip()
            if tr_cont not in ("M", "F"):
                break
            ctx_fk = (last_output2.get("ctx_area_fk100") or "").strip()
            ctx_nk = (last_output2.get("ctx_area_nk100") or "").strip()
            if not ctx_fk and not ctx_nk:
                break

        return {
            "tr_id": tr_id,
            "rt_cd": last_data.get("rt_cd"),
            "msg1": last_data.get("msg1"),
            "output1": all_output1,
            "output2": last_output2,
            "pages": page + 1 if last_data else 0,
        }

    def fetch_overseas_inquire_balance_raw(self, ovrs_excg_cd: str) -> dict:
        """해외 inquire-balance(TTTS3012R) raw — output1·output2. /debug용."""
        data = self._fetch_overseas_inquire_balance(ovrs_excg_cd)
        out1 = data.get("output1") or []
        if not isinstance(out1, list):
            out1 = [out1] if isinstance(out1, dict) else []
        out1 = [dict(x) for x in out1 if isinstance(x, dict)]
        out2 = self._overseas_output2_row(data)
        return {
            "tr_id": "VTTS3012R" if self.is_mock else "TTTS3012R",
            "rt_cd": data.get("rt_cd"),
            "msg1": data.get("msg1"),
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "output1": out1,
            "output2": dict(out2) if out2 else {},
        }

    def _get_domestic_holdings(self) -> list[dict]:
        acct, suffix = self._split_account()
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        tr_id = "VTTC8434R" if self.is_mock else "TTTC8434R"
        ctx_fk, ctx_nk = "", ""
        all_items: list[dict] = []
        self._last_domestic_output2 = {}

        for _page in range(20):
            params = {
                "CANO": acct,
                "ACNT_PRDT_CD": suffix,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": ctx_fk,
                "CTX_AREA_NK100": ctx_nk,
            }
            resp = requests.get(url, headers=self._headers(tr_id), params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if str(data.get("rt_cd", "0")) != "0":
                raise KISBusinessError(data.get("msg1") or "국내 잔고 조회 오류", data)

            for item in data.get("output1") or []:
                row = self._row_from_domestic_item(item)
                if row:
                    all_items.append(row)

            out2 = data.get("output2")
            if isinstance(out2, list) and out2:
                out2 = out2[0] if isinstance(out2[0], dict) else {}
            if not isinstance(out2, dict):
                out2 = {}

            if out2:
                self._last_domestic_output2 = dict(out2)

            tr_cont = (resp.headers.get("tr_cont") or data.get("tr_cont") or "").strip()
            if tr_cont not in ("M", "F"):
                break
            ctx_fk = (out2.get("ctx_area_fk100") or "").strip()
            ctx_nk = (out2.get("ctx_area_nk100") or "").strip()
            if not ctx_fk and not ctx_nk:
                break

        try:
            from app.core.portfolio_adjustment import merge_today_domestic_buys_into_holdings

            today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
            ccld = self._fetch_domestic_daily_ccld(today, today)
            all_items = merge_today_domestic_buys_into_holdings(
                all_items, ccld, self.get_current_price
            )
        except Exception as e:
            logger.warning("당일 매수 잔고 보완 실패: %s", e)

        logger.info("국내 잔고 %d종목 (페이징 포함)", len(all_items))
        return all_items

    def _normalize_overseas_balance_row(self, item: dict, default_ovrs: str) -> Optional[dict]:
        ticker = (item.get("ovrs_pdno") or item.get("pdno") or item.get("symb") or "").strip()
        if not ticker:
            return None
        qty = _kis_int(item, "hldg_qty", "ovrs_stck_evlu_qty", "cblc_qty", "ord_psbl_qty", "hldg_qty_smtl")
        if qty <= 0:
            return None
        name = item.get("prdt_name") or item.get("ovrs_item_name") or item.get("item_name") or ticker
        ocd = (item.get("ovrs_excg_cd") or item.get("tr_mket_cd") or default_ovrs or "NASD").strip().upper()
        qcd = _ORDER_EXCD_TO_QUOTE.get(ocd, "NAS")
        avg_price = _kis_float(item, "pchs_avg_pric", "avg_prc", "frcr_pchs_avg_pric", "pchs_avg_pric")
        current_price = _kis_float(item, "prpr", "now_pric2", "ovrs_now_pric1", "stck_prpr")
        from app.core.account_valuation import (
            _stock_eval_krw_api_row,
            _stock_eval_krw_inquire_row,
            _stock_eval_krw_present_row,
            _stock_eval_usd_row,
        )

        api_fx = _kis_float(item, "bass_exrt", "frst_bltn_exrt", "exrt")
        eval_amount_usd = _stock_eval_usd_row(item)
        if eval_amount_usd <= 0 and qty > 0 and current_price > 0:
            eval_amount_usd = qty * current_price
        eval_amount_krw = _stock_eval_krw_api_row(item, api_fx)
        if eval_amount_krw <= 0:
            eval_amount_krw = (
                _stock_eval_krw_present_row(item, api_fx)
                or _stock_eval_krw_inquire_row(item, api_fx)
            )
        if eval_amount_krw <= 0 and eval_amount_usd > 0 and api_fx > 0:
            eval_amount_krw = round(eval_amount_usd * api_fx)
        purchase_amount = _kis_float(item, "pchs_amt", "frcr_pchs_amt1", "pchs_amt_smtl", "frcr_pchs_amt")
        if purchase_amount <= 0 and qty > 0 and avg_price > 0:
            purchase_amount = qty * avg_price
        profit_rate = _kis_float(item, "evlu_pfls_rt", "ovrs_evlu_pfls_rt", "evlu_pfls_rt")
        if profit_rate == 0 and purchase_amount > 0 and eval_amount_usd > 0:
            profit_rate = (eval_amount_usd - purchase_amount) / purchase_amount * 100
        return {
            "ticker": ticker,
            "name": name,
            "quantity": qty,
            "avg_price": avg_price,
            "current_price": current_price,
            "profit_rate": profit_rate,
            "eval_amount": eval_amount_usd,
            "eval_amount_usd": eval_amount_usd,
            "eval_amount_krw": eval_amount_krw,
            "fx_rate": api_fx,
            "purchase_amount": purchase_amount,
            "currency": "USD",
            "ovrs_excg_cd": ocd,
            "ovrs_quote_excd": qcd,
            "market": _OVRS_MARKET_LABEL.get(ocd, "NASDAQ"),
        }

    @staticmethod
    def _collect_overseas_rows(data: dict) -> list[dict]:
        """체결기준잔고 응답에서 종목 배열 추출 (output1/output2 형태 차이 대응)."""
        rows: list[dict] = []
        for key in ("output1", "output2"):
            block = data.get(key)
            if isinstance(block, list):
                for item in block:
                    if isinstance(item, dict) and (item.get("pdno") or item.get("ovrs_pdno") or item.get("symb")):
                        rows.append(item)
            elif isinstance(block, dict) and (block.get("pdno") or block.get("ovrs_pdno")):
                rows.append(block)
        return rows

    def _fetch_overseas_present_balance_raw(self) -> dict:
        """미국 체결기준 현재잔고 CTRP6504R — 응답 전체."""
        acct, suffix = self._split_account()
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
        tr_id = "VCTRP6504R" if self.is_mock else "CTRP6504R"
        resp = requests.get(
            url,
            headers=self._headers(tr_id),
            params={
                "CANO": acct,
                "ACNT_PRDT_CD": suffix,
                "WCRC_FRCR_DVSN_CD": "02",
                "NATN_CD": "840",
                "TR_MKET_CD": "00",
                "INQR_DVSN_CD": "00",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if str(data.get("rt_cd", "0")) != "0":
            raise KISBusinessError(data.get("msg1") or "해외 체결기준잔고 오류", data)
        self._last_overseas_present_raw = data
        return data

    def _rows_from_present_balance_data(self, data: dict) -> list[dict]:
        raw_rows = self._collect_overseas_rows(data)
        out: list[dict] = []
        seen: set[str] = set()
        for item in raw_rows:
            d = self._normalize_overseas_balance_row(item, item.get("ovrs_excg_cd") or "NASD")
            if not d:
                continue
            key = f"{d['ticker']}:{d['ovrs_excg_cd']}"
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
        return out

    def _get_overseas_us_present_balance(self) -> list[dict]:
        """미국 체결기준 현재잔고 종목 목록."""
        data = self._last_overseas_present_raw
        if not data:
            data = self._fetch_overseas_present_balance_raw()
        out = self._rows_from_present_balance_data(data)
        logger.info("해외(체결기준) 잔고 %d종목", len(out))
        return out

    def _fetch_overseas_inquire_balance(self, ovrs_excg_cd: str) -> dict:
        """해외주식 잔고조회 inquire-balance (거래소별 1회)."""
        acct, suffix = self._split_account()
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        tr_id = "VTTS3012R" if self.is_mock else "TTTS3012R"
        params = {
            "CANO": acct,
            "ACNT_PRDT_CD": suffix,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        resp = requests.get(url, headers=self._headers(tr_id), params=params, timeout=15)
        resp.raise_for_status()
        return _kiss_check_rt_cd(resp.json())

    @staticmethod
    def _overseas_output2_row(data: dict) -> dict:
        out2 = data.get("output2")
        if isinstance(out2, list) and out2:
            return out2[0] if isinstance(out2[0], dict) else {}
        if isinstance(out2, dict):
            return out2
        return {}

    def refresh_overseas_account_valuation(self) -> OverseasValuation:
        """
        해외 총자산(원화): CTRP6504R 체결기준 → 실패 시 TTTS3012R NASD 1회.
        (NASD/NYSE/AMEX 3회 합산은 중복·과대 계상 방지를 위해 사용하지 않음)
        """
        valuation = OverseasValuation.empty()
        present_data = self._last_overseas_present_raw
        if not present_data:
            try:
                present_data = self._fetch_overseas_present_balance_raw()
            except Exception as e:
                logger.warning("해외 체결기준잔고 조회 실패: %s", e)
                present_data = {}

        if present_data:
            valuation = overseas_from_present_balance(present_data)

        if valuation.total_krw <= 0:
            try:
                nasd_data = self._fetch_overseas_inquire_balance("NASD")
                valuation = overseas_from_inquire_balance_nasd(nasd_data)
            except Exception as e:
                logger.warning("해외 inquire-balance(NASD) 폴백 실패: %s", e)

        self._last_overseas_valuation = valuation
        self._last_overseas_eval_krw = valuation.total_krw
        self._last_overseas_eval_detail = {
            "overseas_eval_krw": valuation.total_krw,
            "source": valuation.source,
            "stocks_eval_krw": valuation.stocks_eval_krw,
            "usd_cash_krw": valuation.usd_cash_krw,
            "row_count": valuation.row_count,
            "detail": valuation.detail,
        }
        if valuation.total_krw > 0:
            logger.info(
                "해외 총자산(%s): %s원 (주식 %s + USD예수 %s)",
                valuation.source,
                f"{valuation.total_krw:,.0f}",
                f"{valuation.stocks_eval_krw:,.0f}",
                f"{valuation.usd_cash_krw:,.0f}",
            )
        return valuation

    def refresh_overseas_balance_eval_krw(self) -> dict:
        """하위 호환 — refresh_overseas_account_valuation() 상세 dict."""
        self.refresh_overseas_account_valuation()
        return self._last_overseas_eval_detail

    def get_last_overseas_balance_eval_krw(self) -> float:
        return float(self._last_overseas_eval_krw or 0.0)

    def get_last_overseas_valuation(self) -> OverseasValuation:
        return self._last_overseas_valuation

    def get_account_valuation_snapshot(self, domestic_output2: Optional[dict] = None):
        """국내 output2 + 마지막 해외 평가 → 총자산 스냅샷."""
        from app.core.account_valuation import AccountValuationSnapshot

        o2 = domestic_output2 if domestic_output2 is not None else self._last_domestic_output2
        ov = self._last_overseas_valuation
        if ov.total_krw <= 0:
            ov = self.refresh_overseas_account_valuation()
        return AccountValuationSnapshot.build(o2, ov)

    def _get_overseas_us_ttts_balance(self) -> list[dict]:
        """거래소별 해외 잔고 (NASD/NYSE/AMEX) — 체결기준 API 실패 시 폴백."""
        merged: list[dict] = []
        seen: set[str] = set()
        for ovrs in ("NASD", "NYSE", "AMEX"):
            try:
                data = self._fetch_overseas_inquire_balance(ovrs)
            except Exception as e:
                logger.warning("해외 잔고(%s) HTTP 오류: %s", ovrs, e)
                continue
            for item in data.get("output1") or []:
                row = self._normalize_overseas_balance_row(item, ovrs)
                if not row:
                    continue
                key = f"{row['ticker']}:{row['ovrs_excg_cd']}"
                if key in seen:
                    continue
                seen.add(key)
                merged.append(row)
        logger.info("해외(거래소별) 잔고 %d종목", len(merged))
        return merged

    def _get_overseas_us_holdings(self) -> list[dict]:
        try:
            rows = self._get_overseas_us_present_balance()
            if rows:
                return rows
        except Exception as e:
            logger.info("해외 체결기준잔고 미사용, 거래소별 조회로 폴백: %s", e)
        return self._get_overseas_us_ttts_balance()

    def get_holdings(self) -> list[dict]:
        """국내 + 미국(USD) 보유. 해외는 짧은 간격으로 2회 조회 후 종목 키 기준 병합(누락 완화)."""
        domestic = self._get_domestic_holdings()
        merged_os: dict[str, dict] = {}
        for attempt in range(2):
            try:
                ov = self._get_overseas_us_holdings()
                for r in ov:
                    merged_os[_overseas_balance_row_key(r)] = r
            except Exception as e:
                logger.warning("해외 잔고 조회 실패(시도 %s/2): %s", attempt + 1, e)
            if attempt == 0:
                time.sleep(0.35)
        overseas = list(merged_os.values())
        merged = domestic + overseas
        try:
            self.refresh_overseas_account_valuation()
        except Exception as e:
            logger.warning("해외 총자산 산출 실패: %s", e)
        logger.info("잔고 합계: 국내 %d + 해외(병합) %d = %d", len(domestic), len(overseas), len(merged))
        return merged

    def get_last_domestic_balance_output2(self) -> dict:
        """직전 국내 잔고조회 응답의 output2 (예수금·총평가 등). get_holdings 직후 호출."""
        return dict(self._last_domestic_output2)

    # ─────────────────────────────────────────
    # 현재가 조회
    # ─────────────────────────────────────────
    def get_current_price(self, ticker: str) -> float:
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
        resp = requests.get(url, headers=self._headers("FHKST01010100"), params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return float(data["output"]["stck_prpr"])

    def get_overseas_current_price(self, ticker: str, quote_excd: str) -> float:
        """해외주식 현재가 (EXCD: NAS, NYS, AMS)."""
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        params = {"AUTH": "", "EXCD": quote_excd, "SYMB": ticker}
        resp = requests.get(url, headers=self._headers("HHDFS00000300"), params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        _kiss_check_rt_cd(data)
        out = data.get("output") or {}
        for k in ("last", "t_xprc", "p_xprc", "prpr", "ovrs_nmix_prpr"):
            v = out.get(k)
            if v is not None and str(v).strip() != "":
                return float(str(v).replace(",", ""))
        raise ValueError(f"해외 시세 필드 없음 ticker={ticker} EXCD={quote_excd} output={out}")

    def detect_us_exchange(self, ticker: str) -> tuple[str, str]:
        """
        나스닥/뉴욕/아멕스 중 시세가 나오는 거래소를 찾는다.
        returns: (OVRS_EXCG_CD, quote_EXCD)
        """
        now = time.time()
        hit = self._us_exch_cache.get(ticker)
        if hit and now - hit[2] < 3600:
            return hit[0], hit[1]

        last_err: Optional[Exception] = None
        for q in ("NAS", "NYS", "AMS"):
            try:
                px = self.get_overseas_current_price(ticker, q)
                if px and px > 0:
                    ocd = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}[q]
                    self._us_exch_cache[ticker] = (ocd, q, now)
                    return ocd, q
            except Exception as e:
                last_err = e
                continue
        raise ValueError(f"미국 거래소 시세를 찾을 수 없습니다: {ticker} (마지막 오류: {last_err})") from last_err

    def enrich_holding_trading_meta(self, holding: dict, trailing_cache_entry: Optional[dict] = None) -> dict:
        """
        스케줄/수동 매도용: market, 해외주문 시 ovrs_excg_cd / ovrs_quote_excd 보강.
        """
        if holding.get("currency") == "USD":
            if holding.get("ovrs_excg_cd") and not holding.get("ovrs_quote_excd"):
                holding["ovrs_quote_excd"] = _ORDER_EXCD_TO_QUOTE.get(
                    holding["ovrs_excg_cd"].upper(), "NAS"
                )
            if holding.get("ovrs_excg_cd") and holding.get("ovrs_quote_excd"):
                return holding

        ticker = (holding.get("ticker") or "").strip()
        if holding.get("ovrs_excg_cd") and holding.get("ovrs_quote_excd"):
            return holding

        is_six_digit_kr = ticker.isdigit() and len(ticker) == 6
        region = None
        if trailing_cache_entry:
            region = trailing_cache_entry.get("classification", {}).get("market_region")

        if is_six_digit_kr:
            if not holding.get("market"):
                m = self.get_stock_market_info(ticker)
                holding["market"] = m.get("market", "KOSPI")
            return holding

        if region == "미국" or (region is None and not is_six_digit_kr):
            try:
                ocd, qcd = self.detect_us_exchange(ticker)
                holding["ovrs_excg_cd"] = ocd
                holding["ovrs_quote_excd"] = qcd
                holding["market"] = {"NASD": "NASDAQ", "NYSE": "NYSE", "AMEX": "AMEX"}.get(ocd, "NASDAQ")
                return holding
            except Exception as e:
                logger.warning("%s: 미국 거래소 판별 실패, 국내 조회로 폴백: %s", ticker, e)

        if not holding.get("market"):
            m = self.get_stock_market_info(ticker)
            holding["market"] = m.get("market", "KOSPI")
        return holding

    # ─────────────────────────────────────────
    # 일봉 데이터 조회 (ATR 계산용)
    # ─────────────────────────────────────────
    def get_daily_candles(self, ticker: str, days: int = 20, market: str = "KR") -> list[dict]:
        """
        국내: FHKST03010100
        해외: HHDFS76240000
        반환: [{high, low, close, date}, ...] 최신순
        """
        if market in ("NASDAQ", "NYSE", "AMEX", "NAS", "NYS", "AMS", "NASD"):
            excd = market_to_quote_excd(market)
            return self._get_us_candles(ticker, days, excd)
        return self._get_kr_candles(ticker, days)

    def _get_kr_candles(self, ticker: str, days: int) -> list[dict]:
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days + 10)).strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        }
        resp = requests.get(url, headers=self._headers("FHKST03010100"), params=params, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("output2", [])
        candles = []
        for item in items[:days]:
            candles.append({
                "date": item.get("stck_bsop_date", ""),
                "high": float(item.get("stck_hgpr", "0")),
                "low": float(item.get("stck_lwpr", "0")),
                "close": float(item.get("stck_clpr", "0")),
            })
        return candles

    def _get_us_candles(self, ticker: str, days: int, excd: str) -> list[dict]:
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/dailyprice"
        end = datetime.now().strftime("%Y%m%d")
        params = {
            "AUTH": "",
            "EXCD": excd,
            "SYMB": ticker,
            "GUBN": "0",
            "BYMD": end,
            "MODP": "0",
        }
        resp = requests.get(url, headers=self._headers("HHDFS76240000"), params=params, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("output2", [])
        candles = []
        for item in items[:days]:
            candles.append({
                "date": item.get("xymd", ""),
                "high": float(item.get("high", "0")),
                "low": float(item.get("low", "0")),
                "close": float(item.get("clos", "0")),
            })
        return candles

    def get_stock_market_info(self, ticker: str) -> dict:
        """종목의 시장(KOSPI/KOSDAQ/NASDAQ 등) 및 시가총액 조회"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/search-stock-info"
        params = {"PRDT_TYPE_CD": "300", "PDNO": ticker}
        try:
            resp = requests.get(url, headers=self._headers("CTPF1002R"), params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("output", {})
            mkt_code = data.get("mket_id_cd", "KSP")
            market_map = {"KSP": "KOSPI", "KSQ": "KOSDAQ", "NAS": "NASDAQ", "NYS": "NYSE", "AMS": "AMEX"}
            return {
                "market": market_map.get(mkt_code, mkt_code),
                "market_cap": float(data.get("lstg_stqt", "0")) * float(data.get("last_price", "0")),
            }
        except Exception:
            return {"market": "KOSPI", "market_cap": 0}

    # ─────────────────────────────────────────
    # 매도 주문
    # ─────────────────────────────────────────
    def sell_market_order(self, ticker: str, quantity: int) -> dict:
        """국내 시장가 매도"""
        acct, suffix = self._split_account()
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        tr_id = "VTTC0801U" if self.is_mock else "TTTC0801U"
        body = {
            "CANO": acct,
            "ACNT_PRDT_CD": suffix,
            "PDNO": ticker,
            "ORD_DVSN": "01",        # 01 = 시장가
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0",
            "CTAC_TLNO": "",
            "SLL_TYPE": "01",
            "ALGO_NO": "",
        }
        resp = requests.post(url, headers=self._headers(tr_id), json=body, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        _kiss_check_rt_cd(result)
        logger.info(f"[매도주문] {ticker} {quantity}주 → rt_cd=0")
        return result

    def sell_overseas_market_order(
        self,
        ticker: str,
        quantity: int,
        ovrs_excg_cd: str,
        quote_excd: Optional[str] = None,
    ) -> tuple[dict, float]:
        """
        해외 매도 (모의는 지정가만 가능 → 최근가 지정가로 시장가에 준하게 처리).
        Returns: (API 응답, 주문에 사용한 매도 단가)
        """
        acct, suffix = self._split_account()
        qex = quote_excd or _ORDER_EXCD_TO_QUOTE.get(ovrs_excg_cd.upper(), "NAS")
        price = self.get_overseas_current_price(ticker, qex)
        price_str = f"{price:.2f}"

        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        tr_id = "VTTT1006U" if self.is_mock else "TTTT1006U"
        body = {
            "CANO": acct,
            "ACNT_PRDT_CD": suffix,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "PDNO": ticker,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": price_str,
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",
            "SLL_TYPE": "00",
        }
        resp = requests.post(url, headers=self._headers(tr_id), json=body, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        _kiss_check_rt_cd(result)
        logger.info(f"[해외매도주문] {ticker} {quantity}주 {ovrs_excg_cd} 지정가={price_str}")
        return result, price

    # ─────────────────────────────────────────
    # 거래내역 조회 (수익 계산·잔고 보정용)
    # ─────────────────────────────────────────
    @staticmethod
    def _normalize_domestic_ccld_row(item: dict) -> Optional[dict]:
        ovrs_ticker = (item.get("ovrs_pdno") or "").strip()
        pdno = (item.get("pdno") or "").strip()
        excg = (item.get("excg_dvsn_cd") or item.get("tr_mket_cd") or "").strip().upper()
        _US_EXCG = {"NASD", "NYSE", "AMEX", "NAS", "NYS", "AMS"}
        is_us_row = bool(ovrs_ticker) or excg in _US_EXCG

        ticker = ovrs_ticker or pdno
        if not ticker:
            return None
        qty = _kis_int(item, "tot_ccld_qty")
        if qty <= 0:
            return None
        ord_dt = item.get("ord_dt", "")
        tm = item.get("ord_tmd") or item.get("ccld_tmd") or item.get("infm_tmd") or ""
        ccld_kst = parse_ccld_datetime_kst(ord_dt, tm)
        amt = _kis_float(
            item,
            "frcr_ccld_amt2",
            "frcr_ccld_amt",
            "tot_ccld_amt",
        )
        if amt <= 0:
            pr = _kis_float(item, "avg_prvs", "ccld_unpr", "ft_ccld_unpr")
            amt = pr * qty
        side = (item.get("sll_buy_dvsn_cd") or "").strip()
        ocd = None
        if is_us_row:
            ocd = excg if excg in _US_EXCG else "NASD"
            if ocd in ("NAS", "NASD"):
                ocd = "NASD"
            elif ocd in ("NYS",):
                ocd = "NYSE"
            elif ocd in ("AMS",):
                ocd = "AMEX"
        return {
            "region": "US" if is_us_row else "KR",
            "date": ord_dt,
            "ticker": ticker,
            "name": item.get("prdt_name") or item.get("ovrs_item_name") or ticker,
            "type": "BUY" if side == "02" else "SELL",
            "quantity": qty,
            "price": _kis_float(item, "avg_prvs", "ccld_unpr", "ft_ccld_unpr"),
            "amount": amt,
            "currency": "USD" if is_us_row else "KRW",
            "fx_rate": _kis_float(item, "bass_exrt", "frst_bltn_exrt", "exrt"),
            "ccld_at_kst": ccld_kst,
            "ovrs_excg_cd": ocd,
            "source": "api_domestic",
        }

    @staticmethod
    def _normalize_overseas_ccld_row(item: dict, ovrs_excg_cd: str) -> Optional[dict]:
        ticker = (item.get("ovrs_pdno") or item.get("pdno") or "").strip()
        if not ticker:
            return None
        qty = _kis_int(item, "tot_ccld_qty", "ft_ccld_qty", "ccld_qty")
        if qty <= 0:
            return None
        ord_dt = item.get("ord_dt", "")
        tm = item.get("ord_tmd") or item.get("ccld_tmd") or item.get("infm_tmd") or ""
        ccld_kst = parse_ccld_datetime_kst(ord_dt, tm)
        amt = _kis_float(
            item,
            "frcr_ccld_amt2",
            "frcr_ccld_amt",
            "tot_ccld_amt",
            "ccld_amt",
        )
        if amt <= 0:
            pr = _kis_float(item, "ft_ccld_unpr", "avg_prvs", "ovrs_ccld_unpr")
            amt = pr * qty
        side = (item.get("sll_buy_dvsn_cd") or "").strip()
        return {
            "region": "US",
            "date": ord_dt,
            "ticker": ticker,
            "name": item.get("prdt_name") or item.get("ovrs_item_name") or ticker,
            "type": "BUY" if side == "02" else "SELL",
            "quantity": qty,
            "price": _kis_float(item, "ft_ccld_unpr", "avg_prvs", "ovrs_ccld_unpr"),
            "amount": amt,
            "currency": "USD",
            "fx_rate": _kis_float(item, "bass_exrt", "frst_bltn_exrt", "exrt"),
            "ccld_at_kst": ccld_kst,
            "ovrs_excg_cd": (item.get("ovrs_excg_cd") or ovrs_excg_cd or "NASD").strip().upper(),
            "source": "api_overseas",
        }

    def _fetch_domestic_daily_ccld(self, start: str, end: str) -> list[dict]:
        acct, suffix = self._split_account()
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        tr_id = "VTTC8001R" if self.is_mock else "TTTC8001R"
        params = {
            "CANO": acct,
            "ACNT_PRDT_CD": suffix,
            "INQR_STRT_DT": start,
            "INQR_END_DT": end,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN": "00",
            "PDNO": "",
            "CCLD_DVSN": "01",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        trades: list[dict] = []
        tr_cont = ""
        fk100, nk100 = "", ""
        for _ in range(20):
            params["CTX_AREA_FK100"] = fk100
            params["CTX_AREA_NK100"] = nk100
            resp = requests.get(
                url,
                headers={**self._headers(tr_id), "tr_cont": tr_cont},
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = _kiss_check_rt_cd(resp.json())
            for item in data.get("output1", []) or []:
                row = self._normalize_domestic_ccld_row(item)
                if row:
                    trades.append(row)
            fk100 = (data.get("ctx_area_fk100") or "").strip()
            nk100 = (data.get("ctx_area_nk100") or "").strip()
            tr_cont = (resp.headers.get("tr_cont") or data.get("tr_cont") or "").strip()
            if tr_cont in ("M", "F") and (fk100 or nk100):
                continue
            break
        return trades

    def _fetch_overseas_daily_ccld(self, start: str, end: str, ovrs_excg_cd: str) -> list[dict]:
        acct, suffix = self._split_account()
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-daily-ccld"
        tr_candidates = (
            ["VTTT3035R", "VTTT3018R", "JTTT3035R"]
            if self.is_mock
            else ["TTTS3035R", "TTTS3018R", "JTTT3035R"]
        )
        params = {
            "CANO": acct,
            "ACNT_PRDT_CD": suffix,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "INQR_STRT_DT": start,
            "INQR_END_DT": end,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN": "00",
            "PDNO": "",
            "CCLD_DVSN": "01",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        last_err: Optional[Exception] = None
        for tr_id in tr_candidates:
            trades: list[dict] = []
            tr_cont = ""
            fk100, nk100 = "", ""
            try:
                for _ in range(20):
                    params["CTX_AREA_FK100"] = fk100
                    params["CTX_AREA_NK100"] = nk100
                    resp = requests.get(
                        url,
                        headers={**self._headers(tr_id), "tr_cont": tr_cont},
                        params=params,
                        timeout=15,
                    )
                    resp.raise_for_status()
                    data = _kiss_check_rt_cd(resp.json())
                    for item in data.get("output1", []) or []:
                        row = self._normalize_overseas_ccld_row(item, ovrs_excg_cd)
                        if row:
                            row["source"] = "api_overseas"
                            trades.append(row)
                    fk100 = (data.get("ctx_area_fk100") or "").strip()
                    nk100 = (data.get("ctx_area_nk100") or "").strip()
                    tr_cont = (resp.headers.get("tr_cont") or data.get("tr_cont") or "").strip()
                    if tr_cont in ("M", "F") and (fk100 or nk100):
                        continue
                    break
                return trades
            except Exception as e:
                last_err = e
                logger.debug("해외 체결 TR %s (%s) 실패: %s", tr_id, ovrs_excg_cd, e)
        if last_err:
            raise last_err
        return []

    def get_trade_history(self, days: int = 90) -> list[dict]:
        """국내 체결 내역 (하위 호환)."""
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        try:
            return self._fetch_domestic_daily_ccld(start, end)
        except Exception as e:
            logger.warning("국내 체결내역 조회 실패: %s", e)
            return []

    def get_combined_trade_history(self, days: int = 7) -> list[dict]:
        """국내 + 미국(거래소별) 체결 내역 통합."""
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=max(days, 1))).strftime("%Y%m%d")
        merged: list[dict] = []
        try:
            merged.extend(self._fetch_domestic_daily_ccld(start, end))
        except Exception as e:
            logger.warning("국내 체결내역 조회 실패: %s", e)
        for ovrs in ("NASD", "NYSE", "AMEX"):
            try:
                merged.extend(self._fetch_overseas_daily_ccld(start, end, ovrs))
            except Exception as e:
                logger.warning("해외 체결내역(%s) 조회 실패: %s", ovrs, e)
        return merged


# 싱글톤
_client: Optional[KISApiClient] = None


def get_kis_client() -> KISApiClient:
    global _client
    if _client is None:
        _client = KISApiClient()
    return _client
