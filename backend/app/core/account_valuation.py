"""
한투 Open API 문서 기준 계좌 총자산(원화) 산출.

- 국내 TTTC8434R output2.tot_evlu_amt: 유가증권 평가 + D+2 예수금 (해외 미포함)
- 해외 CTRP6504R(체결기준현재잔고): output3 합계 또는 output1 평가 + output2 외화예수금(원화환산)
- 해외 TTTS3012R 폴백: OVRS_EXCG_CD=NASD 1회 (미국 전체, 거래소 3회 합산 금지)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


def kis_float(item: dict, *keys: str, default: float = 0.0) -> float:
    if not isinstance(item, dict):
        return default
    for k in keys:
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            try:
                return float(str(v).replace(",", ""))
            except ValueError:
                continue
    return default


def _first_dict(block: Any) -> dict:
    if isinstance(block, dict):
        return block
    if isinstance(block, list) and block and isinstance(block[0], dict):
        return block[0]
    return {}


@dataclass
class DomesticValuation:
    tot_evlu_amt: float = 0.0
    dnca_tot_amt: float = 0.0
    scts_evlu_amt: float = 0.0
    nass_amt: float = 0.0
    pchs_amt_smtl_amt: float = 0.0
    evlu_pfls_smtl_amt: float = 0.0
    nrcvb_buy_amt: float = 0.0
    tot_loan_amt: float = 0.0
    tot_stln_slng_chgs: float = 0.0
    source: str = "output2"

    @classmethod
    def from_output2(cls, output2: Optional[dict]) -> "DomesticValuation":
        o = output2 if isinstance(output2, dict) else {}
        return cls(
            tot_evlu_amt=kis_float(o, "tot_evlu_amt"),
            dnca_tot_amt=kis_float(o, "dnca_tot_amt"),
            scts_evlu_amt=kis_float(o, "scts_evlu_amt"),
            nass_amt=kis_float(o, "nass_amt"),
            pchs_amt_smtl_amt=kis_float(o, "pchs_amt_smtl_amt"),
            evlu_pfls_smtl_amt=kis_float(o, "evlu_pfls_smtl_amt"),
            nrcvb_buy_amt=kis_float(o, "nrcvb_buy_amt"),
            tot_loan_amt=kis_float(o, "tot_loan_amt"),
            tot_stln_slng_chgs=kis_float(o, "tot_stln_slng_chgs"),
            source="output2",
        )

    def base_krw(self) -> tuple[float, str]:
        """국내 총평가(원화). tot_evlu_amt 우선, 없으면 예수금+주식평가."""
        if self.tot_evlu_amt > 0:
            return self.tot_evlu_amt, "domestic_tot_evlu_amt"
        parts = self.dnca_tot_amt + self.scts_evlu_amt
        if parts > 0:
            return parts, "domestic_dnca_plus_scts"
        if self.nass_amt > 0:
            return self.nass_amt, "domestic_nass_amt"
        return 0.0, "domestic_empty"


@dataclass
class OverseasValuation:
    total_krw: float = 0.0
    stocks_eval_krw: float = 0.0
    stocks_eval_usd: float = 0.0
    usd_cash_krw: float = 0.0
    usd_cash_usd: float = 0.0
    fx_rate: float = 0.0
    source: str = ""
    row_count: int = 0
    detail: dict = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "OverseasValuation":
        return cls(source="none")


def _kis_float_present_qty(item: dict) -> float:
    return kis_float(
        item,
        "ccld_qty_smtl1",
        "ord_psbl_qty1",
        "ccld_qty13",
        "hldg_qty",
        "ovrs_cblc_qty",
    )


def _row_fx_rate(item: dict, fallback_fx: float = 0.0) -> float:
    rate = kis_float(item, "bass_exrt", "frst_bltn_exrt", "exrt")
    return rate if rate > 0 else max(0.0, float(fallback_fx or 0))


def _stock_eval_usd_row(item: dict) -> float:
    """해외 종목 평가액(USD). frcr_* 우선 — evlu_amt 단독은 USD·원화 혼동."""
    frcr = kis_float(item, "frcr_evlu_amt2", "frcr_evlu_amt", "frcr_evlu_pfls_amt2")
    if frcr > 0:
        return frcr
    qty = _kis_float_present_qty(item) or kis_float(item, "ovrs_cblc_qty", "hldg_qty")
    price = kis_float(item, "ovrs_now_pric1", "now_pric2", "prpr")
    if qty > 0 and price > 0:
        return qty * price
    evlu = kis_float(item, "evlu_amt", "ovrs_stck_evlu_amt")
    if evlu > 0 and evlu < 1_000_000:
        return evlu
    return 0.0


def _stock_eval_krw_api_row(item: dict, fallback_fx: float = 0.0) -> float:
    """
    한투 API 원화 환산값.
    - wcrc_evlu_amt: 원화 확정
    - evlu_amt: 50만 미만이면 USD로 보고 행 bass_exrt 로 원화 환산 (외부 환율 API 미사용)
    - evlu_amt >= 50만: 이미 원화로 간주
    """
    wcrc = kis_float(item, "wcrc_evlu_amt")
    if wcrc > 0:
        return wcrc

    rate = _row_fx_rate(item, fallback_fx)
    evlu = kis_float(item, "evlu_amt", "ovrs_stck_evlu_amt")
    if evlu <= 0:
        return 0.0
    if evlu >= 500_000:
        return evlu
    if rate > 0:
        return round(evlu * rate)

    usd = _stock_eval_usd_row(item)
    if usd > 0 and fallback_fx > 0:
        return round(usd * fallback_fx)
    return 0.0


def _stock_eval_krw_present_row(item: dict, fallback_fx: float = 0.0) -> float:
    """CTRP6504R output1 — API wcrc/evlu_amt(원화) 우선."""
    api_krw = _stock_eval_krw_api_row(item, fallback_fx)
    if api_krw > 0:
        return api_krw

    usd = _stock_eval_usd_row(item)
    rate = _row_fx_rate(item, fallback_fx)
    if usd > 0 and rate > 0:
        return round(usd * rate)

    return 0.0


def _usd_deposit_from_present_output2(output2: Any) -> tuple[float, float, float]:
    """returns (usd, krw, fx_rate) — CTRP6504R output2 리스트 또는 TTTS3012R 단일 dict."""
    usd_sum = 0.0
    krw_sum = 0.0
    rate_used = 0.0
    rows = output2 if isinstance(output2, list) else ([output2] if isinstance(output2, dict) else [])
    for dep in rows:
        if not isinstance(dep, dict):
            continue
        ccy = (dep.get("crcy_cd") or dep.get("tr_crcy_cd") or "").strip().upper()
        usd = kis_float(
            dep,
            "frcr_dncl_amt_2",
            "frcr_dncl_amt",
            "frcr_dnca_tot_amt",
            "ovrs_frcr_dncl_amt",
            "dncl_amt",
        )
        rate = kis_float(dep, "frst_bltn_exrt", "bass_exrt", "exrt")
        if ccy and ccy not in ("USD", "840", ""):
            continue
        if usd <= 0:
            continue
        usd_sum += usd
        if rate > 0:
            rate_used = rate
            krw_sum += usd * rate
        else:
            logger.warning("해외 USD 예수금 환율 없음: %s USD", usd)
    return usd_sum, krw_sum, rate_used


def overseas_cash_from_valuation(overseas: Optional["OverseasValuation"], fx_rate: float = 0.0) -> tuple[float, float]:
    """(usd, krw) 외화예수금."""
    if overseas is None:
        return 0.0, 0.0
    usd = max(0.0, float(overseas.usd_cash_usd or 0))
    krw = max(0.0, float(overseas.usd_cash_krw or 0))
    fx = max(0.0, float(overseas.fx_rate or fx_rate or 0))
    if krw <= 0 and usd > 0 and fx > 0:
        krw = usd * fx
    if usd <= 0 and krw > 0 and fx > 0:
        usd = krw / fx
    return round(usd, 2), round(krw)


def _usd_deposit_krw_from_present_output2(output2: Any) -> float:
    return _usd_deposit_from_present_output2(output2)[1]


def _fx_rate_from_blocks(output3: Any, output2: Any) -> float:
    o3 = _first_dict(output3)
    for block in (o3, _first_dict(output2)):
        rate = kis_float(block, "bass_exrt", "frst_bltn_exrt", "exrt")
        if rate > 0:
            return rate
    rows = output2 if isinstance(output2, list) else ([output2] if isinstance(output2, dict) else [])
    for dep in rows:
        if isinstance(dep, dict):
            rate = kis_float(dep, "frst_bltn_exrt", "bass_exrt", "exrt")
            if rate > 0:
                return rate
    return 0.0


def _total_from_present_output3(output3: Any, output2: Any = None) -> tuple[float, str]:
    """output3 — wcrc_* 만 원화로 인정. tot_evlu_amt(무접두)는 USD인 경우가 많아 제외."""
    o3 = _first_dict(output3)
    if not o3:
        return 0.0, ""
    rate = _fx_rate_from_blocks(output3, output2)

    for key in ("wcrc_tot_evlu_amt", "wcrc_evlu_amt_smtl", "tot_asst_evlu_amt"):
        v = kis_float(o3, key)
        if v > 0:
            return v, key

    frcr = kis_float(o3, "frcr_evlu_amt_smtl", "evlu_amt_smtl", "frcr_buy_amt_smtl")
    if frcr > 0 and rate > 0:
        return frcr * rate, "frcr_evlu_amt_smtl_x_rate"
    return 0.0, ""


def _pick_overseas_total(
    stocks_krw: float,
    cash_krw: float,
    o3_total: float,
    o3_key: str,
) -> tuple[float, str]:
    """종목(원화)+USD예수만 총자산에 사용. output3 단독·USD 오인 합계는 사용 안 함."""
    computed = stocks_krw + cash_krw
    if computed > 0:
        return computed, "present_stocks_plus_cash"
    if o3_total > 0 and o3_key.startswith("wcrc"):
        return o3_total, f"present_output3_{o3_key}"
    if o3_total > 0 and o3_key.endswith("_x_rate"):
        return o3_total, f"present_output3_{o3_key}"
    return 0.0, ""


def overseas_from_present_balance(data: dict) -> OverseasValuation:
    """CTRP6504R / VCTRP6504R 응답 → 해외 총자산(원화)."""
    if not isinstance(data, dict):
        return OverseasValuation.empty()

    cash_usd, cash_krw, cash_fx = _usd_deposit_from_present_output2(data.get("output2"))
    o3_total, o3_key = _total_from_present_output3(data.get("output3"), data.get("output2"))
    fx_rate = cash_fx or _fx_rate_from_blocks(data.get("output3"), data.get("output2"))
    if fx_rate <= 0:
        from app.services.fx_rate import get_usd_krw_rate

        fx_rate = float(get_usd_krw_rate() or 0)

    stocks_krw = 0.0
    stocks_usd = 0.0
    row_count = 0
    for item in data.get("output1") or []:
        if not isinstance(item, dict):
            continue
        ticker = (item.get("pdno") or item.get("ovrs_pdno") or "").strip()
        if not ticker:
            continue
        qty = _kis_float_present_qty(item)
        rate_row = _row_fx_rate(item, fx_rate)
        usd_row = _stock_eval_usd_row(item)
        ev_krw = _stock_eval_krw_present_row(item, fx_rate)
        if ev_krw > 0 or usd_row > 0:
            stocks_usd += usd_row
            stocks_krw += ev_krw
            row_count += 1
        elif qty <= 0:
            continue

    total, source = _pick_overseas_total(stocks_krw, cash_krw, o3_total, o3_key)
    if total <= 0:
        return OverseasValuation.empty()

    return OverseasValuation(
        total_krw=total,
        stocks_eval_krw=stocks_krw if stocks_krw > 0 else max(0.0, total - cash_krw),
        stocks_eval_usd=stocks_usd,
        usd_cash_krw=cash_krw,
        usd_cash_usd=cash_usd,
        fx_rate=fx_rate,
        source=source,
        row_count=row_count,
        detail={
            "output3_tot": o3_total,
            "output3_key": o3_key,
            "stocks_sum": stocks_krw,
            "stocks_usd": stocks_usd,
            "usd_cash_krw": cash_krw,
            "usd_cash_usd": cash_usd,
            "computed_sum": stocks_krw + cash_krw,
        },
    )


def _stock_eval_krw_inquire_row(item: dict, fallback_fx: float = 0.0) -> float:
    """TTTS3012R output1 — wcrc/evlu_amt + API bass_exrt 원화 환산."""
    api_krw = _stock_eval_krw_api_row(item, fallback_fx)
    if api_krw > 0:
        return api_krw

    usd = kis_float(item, "frcr_evlu_amt2", "frcr_evlu_amt")
    if usd <= 0:
        qty = kis_float(item, "ovrs_cblc_qty", "hldg_qty")
        price = kis_float(item, "now_pric2", "prpr")
        if qty > 0 and price > 0:
            usd = qty * price

    rate = _row_fx_rate(item, fallback_fx)
    if usd > 0 and rate > 0:
        return round(usd * rate)
    return 0.0


def usd_to_krw(usd: float, fx_rate: float, krw_hint: float = 0.0) -> float:
    """USD→원화. krw_hint가 USD와 거의 같으면(환율 미적용) 재계산."""
    usd = max(0.0, float(usd or 0))
    fx = max(0.0, float(fx_rate or 0))
    if usd <= 0:
        return max(0.0, float(krw_hint or 0))
    if fx <= 0:
        return max(0.0, float(krw_hint or 0))
    expected = usd * fx
    hint = max(0.0, float(krw_hint or 0))
    if hint <= 0:
        return expected
    if hint <= usd * 1.02:
        return expected
    if abs(hint - expected) / expected <= 0.12:
        return hint
    return expected


def overseas_from_inquire_balance_nasd(data: dict) -> OverseasValuation:
    """TTTS3012R NASD 1회 — output2.tot_evlu_amt(원화) 또는 output1 합."""
    if not isinstance(data, dict):
        return OverseasValuation.empty()

    o2 = _first_dict(data.get("output2"))
    o2_tot = kis_float(o2, "tot_evlu_amt", "ovrs_tot_evlu_amt", "tot_evlu")
    frcr_dnca = kis_float(o2, "frcr_dnca_tot_amt", "frcr_dncl_amt_2", "dnca_tot_amt")
    frcr_rate = kis_float(o2, "bass_exrt", "exrt", "frst_bltn_exrt")
    cash_krw = frcr_dnca * frcr_rate if frcr_dnca > 0 and frcr_rate > 0 else 0.0

    stocks_krw = 0.0
    stocks_usd = 0.0
    row_count = 0
    for item in data.get("output1") or []:
        if not isinstance(item, dict):
            continue
        ticker = (item.get("ovrs_pdno") or item.get("pdno") or "").strip()
        qty = kis_float(item, "ovrs_cblc_qty", "hldg_qty")
        if not ticker or qty <= 0:
            continue
        usd_row = _stock_eval_usd_row(item)
        ev = _stock_eval_krw_inquire_row(item, frcr_rate)
        if usd_row > 0 or ev > 0:
            stocks_usd += usd_row
            if ev > 0:
                stocks_krw += ev
            row_count += 1

    computed = stocks_krw + cash_krw
    if computed > 0:
        total, source = _pick_overseas_total(stocks_krw, cash_krw, o2_tot, "output2_tot")
        return OverseasValuation(
            total_krw=total,
            stocks_eval_krw=stocks_krw,
            stocks_eval_usd=stocks_usd,
            usd_cash_krw=cash_krw,
            usd_cash_usd=frcr_dnca if frcr_dnca > 0 else 0.0,
            fx_rate=frcr_rate if frcr_rate > 0 else 0.0,
            source=f"inquire_nasd_{source}",
            row_count=row_count,
            detail={
                "output2_tot": o2_tot,
                "frcr_dnca": frcr_dnca,
                "stocks_sum": stocks_krw,
                "computed_sum": computed,
            },
        )
    if o2_tot > 0:
        return OverseasValuation(
            total_krw=o2_tot,
            stocks_eval_krw=stocks_krw,
            stocks_eval_usd=stocks_usd,
            usd_cash_krw=cash_krw,
            usd_cash_usd=frcr_dnca if frcr_dnca > 0 else 0.0,
            fx_rate=frcr_rate if frcr_rate > 0 else 0.0,
            source="inquire_nasd_output2_tot_only",
            row_count=row_count,
            detail={"output2_tot": o2_tot, "frcr_dnca": frcr_dnca},
        )
    return OverseasValuation.empty()


@dataclass
class AccountValuationSnapshot:
    domestic: DomesticValuation
    overseas: OverseasValuation
    domestic_base_krw: float = 0.0
    overseas_base_krw: float = 0.0
    total_before_adjustment_krw: float = 0.0
    method: str = ""

    @classmethod
    def build(
        cls,
        domestic_output2: Optional[dict],
        overseas: OverseasValuation,
    ) -> "AccountValuationSnapshot":
        dom = DomesticValuation.from_output2(domestic_output2)
        dom_krw, dom_method = dom.base_krw()
        ov_krw = max(0.0, overseas.total_krw)
        total = dom_krw + ov_krw
        method = f"{dom_method}+{overseas.source or 'overseas_none'}"
        return cls(
            domestic=dom,
            overseas=overseas,
            domestic_base_krw=dom_krw,
            overseas_base_krw=ov_krw,
            total_before_adjustment_krw=total,
            method=method,
        )


def holdings_domestic_stocks_krw(holdings: list[dict]) -> float:
    total = 0.0
    for h in holdings or []:
        if h.get("currency") == "USD":
            continue
        total += float(h.get("eval_amount") or 0)
    return round(total)


def reconcile_domestic_stocks_krw(
    api_scts_evlu_amt: float,
    holdings: Optional[list[dict]],
) -> tuple[float, str]:
    """output2 주식평가가 보유 합계보다 작으면 보유 기준(당일 매수 반영)."""
    from_holdings = holdings_domestic_stocks_krw(holdings or [])
    api_val = max(0.0, float(api_scts_evlu_amt or 0))
    if from_holdings <= 0:
        if api_val > 0:
            return round(api_val), "api_scts_only"
        return 0.0, "none"
    if api_val <= 0 or api_val < from_holdings * 0.98:
        return from_holdings, "holdings_eval_domestic"
    return round(api_val), "api_scts_ok"


def holdings_overseas_eval_usd(holdings: list[dict]) -> float:
    """USD 종목 평가 합(달러)."""
    total = 0.0
    for h in holdings or []:
        if h.get("currency") != "USD":
            continue
        usd = float(h.get("eval_amount_usd") or h.get("eval_amount") or 0)
        total += usd
    return round(total, 2)


def holdings_overseas_eval_krw(holdings: list[dict], fx_rate: float = 0.0) -> float:
    """보유 USD 종목 평가 합(원화) — eval_amount_krw, 없으면 USD×API(fx_rate) 행 환율."""
    if not holdings:
        return 0.0
    total = 0.0
    for h in holdings:
        if h.get("currency") != "USD":
            continue
        usd = float(h.get("eval_amount_usd") or h.get("eval_amount") or 0)
        krw = float(h.get("eval_amount_krw") or 0)
        row_fx = float(h.get("fx_rate") or fx_rate or 0)
        if krw > 0 and usd > 0 and krw <= usd * 1.05 and row_fx > 0:
            krw = round(usd * row_fx)
        elif (krw <= 0 or krw <= usd * 1.05) and usd > 0 and row_fx > 0:
            krw = round(usd * row_fx)
        total += max(0.0, krw)
    return round(total)


def reconcile_overseas_base_krw(
    api_ov_krw: float,
    usd_cash_krw: float,
    holdings: Optional[list[dict]],
    fx_rate: Optional[float],
) -> tuple[float, str]:
    """API 해외 합계가 보유 원화평가보다 작으면 보유 기준으로 보정."""
    from_holdings = holdings_overseas_eval_krw(holdings or [], float(fx_rate or 0))
    computed = from_holdings + max(0.0, float(usd_cash_krw or 0))
    api_total = max(0.0, float(api_ov_krw or 0))

    if computed <= 0:
        if api_total > 0:
            return api_total, "api_only"
        return 0.0, "none"

    if api_total <= 0 or api_total < computed * 0.85:
        return round(computed), "holdings_eval_krw_plus_usd_cash"
    return round(api_total), "api_ok"


def build_asset_breakdown(
    domestic: DomesticValuation,
    overseas: OverseasValuation,
    holdings: Optional[list[dict]],
    fx_rate: Optional[float],
    pending_sell_krw: float = 0.0,
    pending_buy_krw: float = 0.0,
    *,
    pending_sell_settlement_krw: float = 0.0,
    pending_sell_settlement_dom_krw: float = 0.0,
    pending_sell_settlement_ov_krw: float = 0.0,
    nrcvb_buy_amt_krw: float = 0.0,
    credit_loan_krw: float = 0.0,
) -> dict:
    """
    추정 총자산 =
      국내주식 + 국내예수금 + 해외주식(원, API evlu_amt) + 해외예수금(원)
      + 매도미결제(T+2/T+1) − 미수매수(nrcvb) − 신용대출(tot_loan)
    """
    fx = float(fx_rate or overseas.fx_rate or 0)

    dom_cash = max(0.0, domestic.dnca_tot_amt)
    dom_stocks, _dom_src = reconcile_domestic_stocks_krw(domestic.scts_evlu_amt, holdings)
    if domestic.tot_evlu_amt > 0:
        implied_stocks = max(0.0, domestic.tot_evlu_amt - dom_cash)
        if implied_stocks > dom_stocks:
            dom_stocks = implied_stocks

    ov_stocks_krw = max(0.0, overseas.stocks_eval_krw)
    ov_stocks_usd = max(0.0, overseas.stocks_eval_usd)
    if holdings:
        h_krw = holdings_overseas_eval_krw(holdings, fx)
        h_usd = holdings_overseas_eval_usd(holdings)
        if h_krw > 0:
            ov_stocks_krw = h_krw
        elif h_usd > 0 and fx > 0:
            ov_stocks_krw = round(h_usd * fx)
        if h_usd > 0:
            ov_stocks_usd = h_usd

    ov_cash_usd, ov_cash_krw = overseas_cash_from_valuation(overseas, fx)

    pending_settle = max(0.0, float(pending_sell_settlement_krw or 0))
    pending_settle_dom = max(0.0, float(pending_sell_settlement_dom_krw or 0))
    pending_settle_ov = max(0.0, float(pending_sell_settlement_ov_krw or 0))
    if pending_settle <= 0 and (pending_settle_dom > 0 or pending_settle_ov > 0):
        pending_settle = pending_settle_dom + pending_settle_ov

    legacy_pending = max(0.0, float(pending_sell_krw or 0))
    pending_buy = max(0.0, float(pending_buy_krw or 0))
    nrcvb = max(0.0, float(nrcvb_buy_amt_krw or domestic.nrcvb_buy_amt or 0))
    credit_loan = max(0.0, float(credit_loan_krw or domestic.tot_loan_amt or 0))
    deductions = nrcvb + credit_loan

    subtotal = dom_stocks + dom_cash + ov_stocks_krw + ov_cash_krw
    total = subtotal + pending_settle + pending_buy + legacy_pending - deductions

    return {
        "domestic_stocks_krw": round(dom_stocks),
        "domestic_cash_krw": round(dom_cash),
        "overseas_stocks_usd": round(ov_stocks_usd, 2),
        "overseas_stocks_krw": round(ov_stocks_krw),
        "overseas_cash_usd": round(ov_cash_usd, 2),
        "overseas_cash_krw": round(ov_cash_krw),
        "subtotal_krw": round(subtotal),
        "pending_sell_settlement_krw": round(pending_settle),
        "pending_sell_settlement_dom_krw": round(pending_settle_dom),
        "pending_sell_settlement_ov_krw": round(pending_settle_ov),
        "nrcvb_buy_amt_krw": round(nrcvb),
        "credit_loan_krw": round(credit_loan),
        "deductions_krw": round(deductions),
        "pending_sell_krw": round(legacy_pending or pending_settle),
        "pending_buy_krw": round(pending_buy),
        "total_net_worth_krw": round(total),
        "fx_usd_krw": round(fx, 2) if fx else None,
        "overseas_stocks_source": "api_evlu_x_bass_exrt",
    }


def resolve_account_base_from_snapshot(
    snapshot: AccountValuationSnapshot,
    holdings: Optional[list] = None,
    fx_rate: Optional[float] = None,
) -> dict:
    """portfolio_adjustment.resolve_account_base_krw 호환 dict."""
    dom = snapshot.domestic
    ov = snapshot.overseas
    dom_krw, dom_method = dom.base_krw()
    ov_krw = float(ov.total_krw or 0)

    ov_krw, ov_src = reconcile_overseas_base_krw(
        ov_krw,
        ov.usd_cash_krw,
        holdings,
        fx_rate,
    )
    if ov_src not in ("api_ok", "none") and ov_src:
        ov = OverseasValuation(
            total_krw=ov_krw,
            stocks_eval_krw=holdings_overseas_eval_krw(holdings or [], float(fx_rate or 0)),
            usd_cash_krw=ov.usd_cash_krw,
            source=ov_src,
        )

    account_base = dom_krw + ov_krw
    method = snapshot.method
    if ov_src == "holdings_eval_krw_plus_usd_cash":
        method = f"{dom_method}+{ov_src}"
    elif ov_src and ov_src != "api_ok":
        method = f"{method}+{ov_src}"

    breakdown = build_asset_breakdown(dom, ov, holdings, fx_rate, pending_sell_krw=0.0)
    account_base = breakdown["subtotal_krw"]

    return {
        "account_base_krw": account_base,
        "domestic_base_krw": breakdown["domestic_stocks_krw"] + breakdown["domestic_cash_krw"],
        "overseas_base_krw": breakdown["overseas_stocks_krw"] + breakdown["overseas_cash_krw"],
        "account_base_method": method,
        "cash_deposit_krw": dom.dnca_tot_amt,
        "domestic_tot_evlu_krw": dom.tot_evlu_amt,
        "stocks_eval_domestic_krw": breakdown["domestic_stocks_krw"],
        "stocks_eval_overseas_krw": breakdown["overseas_stocks_krw"],
        "stocks_eval_total_krw": breakdown["domestic_stocks_krw"] + breakdown["overseas_stocks_krw"],
        "tot_evlu_amt_raw": dom.tot_evlu_amt,
        "overseas_eval_from_api_krw": ov.total_krw,
        "overseas_usd_cash_krw": breakdown["overseas_cash_krw"],
        "overseas_valuation_source": ov.source,
        "asset_breakdown": breakdown,
    }
