"""
한국투자증권 API 클라이언트
실전/모의 투자 모두 지원
"""
import os
import time
import requests
from datetime import datetime, timedelta

from typing import Optional
import logging

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
        qty = _kis_int(item, "hldg_qty")
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
                "PRCS_DVSN": "01",
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
        eval_amount = _kis_float(item, "evlu_amt", "ovrs_stck_evlu_amt", "frcr_evlu_amt2")
        purchase_amount = _kis_float(item, "pchs_amt", "frcr_pchs_amt1", "pchs_amt_smtl", "frcr_pchs_amt")
        if eval_amount <= 0 and qty > 0 and current_price > 0:
            eval_amount = qty * current_price
        if purchase_amount <= 0 and qty > 0 and avg_price > 0:
            purchase_amount = qty * avg_price
        profit_rate = _kis_float(item, "evlu_pfls_rt", "ovrs_evlu_pfls_rt", "evlu_pfls_rt")
        if profit_rate == 0 and purchase_amount > 0:
            profit_rate = (eval_amount - purchase_amount) / purchase_amount * 100
        return {
            "ticker": ticker,
            "name": name,
            "quantity": qty,
            "avg_price": avg_price,
            "current_price": current_price,
            "profit_rate": profit_rate,
            "eval_amount": eval_amount,
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

    def _get_overseas_us_present_balance(self) -> list[dict]:
        """미국 체결기준 현재잔고 (한 번에 조회, NATN_CD=840)."""
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
        logger.info("해외(체결기준) 잔고 %d종목 (raw %d행)", len(out), len(raw_rows))
        return out

    def _get_overseas_us_ttts_balance(self) -> list[dict]:
        """거래소별 해외 잔고 (NASD/NYSE/AMEX) — 체결기준 API 실패 시 폴백."""
        acct, suffix = self._split_account()
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        tr_id = "VTTS3012R" if self.is_mock else "TTTS3012R"
        merged: list[dict] = []
        seen: set[str] = set()
        for ovrs in ("NASD", "NYSE", "AMEX"):
            params = {
                "CANO": acct,
                "ACNT_PRDT_CD": suffix,
                "OVRS_EXCG_CD": ovrs,
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            }
            try:
                resp = requests.get(url, headers=self._headers(tr_id), params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning("해외 잔고(%s) HTTP 오류: %s", ovrs, e)
                continue
            if str(data.get("rt_cd", "0")) != "0":
                logger.warning("해외 잔고(%s) API: %s", ovrs, data.get("msg1"))
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
    ) -> dict:
        """
        해외 매도 (모의는 지정가만 가능 → 최근가 지정가로 시장가에 준하게 처리).
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
        return result

    # ─────────────────────────────────────────
    # 거래내역 조회 (수익 계산용)
    # ─────────────────────────────────────────
    def get_trade_history(self, days: int = 90) -> list[dict]:
        """최근 N일 체결 내역"""
        acct, suffix = self._split_account()
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        tr_id = "VTTC8001R" if self.is_mock else "TTTC8001R"
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
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
        resp = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        trades = []
        for item in data.get("output1", []):
            trades.append({
                "date": item.get("ord_dt", ""),
                "ticker": item.get("pdno", ""),
                "name": item.get("prdt_name", ""),
                "type": "BUY" if item.get("sll_buy_dvsn_cd") == "02" else "SELL",
                "quantity": int(item.get("tot_ccld_qty", "0")),
                "price": float(item.get("avg_prvs", "0")),
                "amount": float(item.get("tot_ccld_amt", "0")),
            })
        return trades


# 싱글톤
_client: Optional[KISApiClient] = None


def get_kis_client() -> KISApiClient:
    global _client
    if _client is None:
        _client = KISApiClient()
    return _client
