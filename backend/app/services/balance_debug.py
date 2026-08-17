"""
KIS 잔고 API raw 응답 수집·포맷 (/debug, 서버 로그).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 텔레그램 메시지 상한(여유)
_TELEGRAM_CHUNK = 3800

# 종목별 output1 에서 우선 표시할 키 (없으면 전체 JSON)
_DOMESTIC_OUTPUT2_KEYS = (
    "dnca_tot_amt",
    "scts_evlu_amt",
    "tot_evlu_amt",
    "nass_amt",
    "thdt_sll_amt",
    "thdt_buy_amt",
    "bfdy_sll_amt",
    "bfdy_buy_amt",
    "prvs_rcdl_excc_amt",
    "pchs_amt_smtl_amt",
    "evlu_pfls_smtl_amt",
    "nrcvb_buy_amt",
    "tot_loan_amt",
    "tot_stln_slng_chgs",
    "nxdy_excc_amt",
    "d2_auto_rdpt_amt",
)

_DOMESTIC_ROW_KEYS = (
    "pdno",
    "prdt_name",
    "hldg_qty",
    "ord_psbl_qty",
    "pchs_avg_pric",
    "pchs_amt",
    "prpr",
    "evlu_amt",
    "evlu_pfls_amt",
    "evlu_pfls_rt",
    "fltt_rt",
)
_OVERSEAS_ROW_KEYS = (
    "ovrs_pdno",
    "pdno",
    "prdt_name",
    "ovrs_item_name",
    "ovrs_excg_cd",
    "hldg_qty",
    "ovrs_cblc_qty",
    "cbld_qty_smtl1",
    "pchs_avg_pric",
    "pchs_amt",
    "frcr_pchs_amt1",
    "prpr",
    "now_pric2",
    "ovrs_now_pric1",
    "evlu_amt",
    "frcr_evlu_amt2",
    "frcr_evlu_amt",
    "ovrs_stck_evlu_amt",
    "wcrc_evlu_amt",
    "bass_exrt",
    "evlu_pfls_rt",
    "loan_amt",
    "stln_slng_chgs",
)


def _json_pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _pick_fields(row: dict, keys: tuple[str, ...]) -> dict:
    if not isinstance(row, dict):
        return {}
    out = {k: row[k] for k in keys if k in row and row[k] is not None and str(row[k]).strip() != ""}
    extra = {k: v for k, v in row.items() if k not in out}
    if extra:
        out["_other_fields"] = extra
    return out if out else dict(row)


def _format_output2_block(title: str, o2: Any) -> str:
    lines = [f"{'=' * 40}", title, f"{'=' * 40}"]
    if isinstance(o2, list):
        for i, block in enumerate(o2):
            lines.append(f"--- [output2 index {i}] ---")
            lines.append(_json_pretty(block if isinstance(block, dict) else block))
    elif isinstance(o2, dict) and o2:
        for k, v in sorted(o2.items(), key=lambda x: x[0]):
            lines.append(f"{k}: {v}")
        lines.append("")
        lines.append("(raw JSON)")
        lines.append(_json_pretty(o2))
    else:
        lines.append("(empty)")
    return "\n".join(lines)


def _format_output1_rows(title: str, rows: list, keys: tuple[str, ...]) -> str:
    lines = [f"{'=' * 40}", title, f"count={len(rows)}", f"{'=' * 40}"]
    if not rows:
        lines.append("(no rows)")
        return "\n".join(lines)
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        ticker = row.get("pdno") or row.get("ovrs_pdno") or row.get("symb") or f"#{i}"
        lines.append(f"--- [{i}] {ticker} ---")
        lines.append(_json_pretty(_pick_fields(row, keys)))
    return "\n".join(lines)


def _fmt_krw(v: float) -> str:
    return f"{round(float(v or 0)):,}원"


def _sum_domestic_output1_loan(rows: list) -> float:
    total = 0.0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        total += float(str(row.get("loan_amt") or "0").replace(",", "") or 0)
    return round(total)


def build_hts_comparison_section(client, snapshot: dict) -> str:
    """한투 앱 총자산 1:1 비교용 항목별 원화."""
    from app.core.account_valuation import kis_float
    from app.core.portfolio_adjustment import (
        calc_pending_sell_settlement_krw,
        merge_trade_sources,
        parse_domestic_liabilities_krw,
        trades_from_sell_log,
    )
    from app.core.sell_engine import calc_portfolio_summary
    from app.core.state import get_settings
    from app.services.fx_rate import get_usd_krw_rate

    settings = get_settings()
    seed = settings.get("seed_money", 1_000_000)
    dom = snapshot.get("domestic") or {}
    o2 = dom.get("output2") if isinstance(dom.get("output2"), dict) else {}
    o1 = dom.get("output1") or []

    try:
        holdings = client.get_holdings()
    except Exception as e:
        holdings = []
        logger.warning("비교용 holdings 조회 실패: %s", e)

    fx = get_usd_krw_rate()
    trades: list = []
    try:
        api_trades = client.get_combined_trade_history(days=14)
        log_trades = trades_from_sell_log(max_days=14)
        trades = merge_trade_sources(log_trades, api_trades)
    except Exception as e:
        logger.warning("비교용 체결내역 조회 실패: %s", e)

    pending_total, pending_detail = calc_pending_sell_settlement_krw(trades, fx)
    liabilities = parse_domestic_liabilities_krw(o2)
    loan_o1 = _sum_domestic_output1_loan(o1)

    summary = calc_portfolio_summary(holdings, seed, trades, o2, fetch_trades_for_adjustment=False)
    b = summary.get("asset_breakdown") or {}

    lines = [
        "=" * 40,
        "0) 한투 앱 비교용 — 추정 총자산 구성 (원화)",
        "=" * 40,
        f"국내 주식평가     {_fmt_krw(b.get('domestic_stocks_krw', 0))}",
        f"국내 예수금       {_fmt_krw(b.get('domestic_cash_krw', 0))}",
        f"해외 주식평가     {_fmt_krw(b.get('overseas_stocks_krw', 0))}  (API evlu_amt 합)",
        f"해외 예수금       {_fmt_krw(b.get('overseas_cash_krw', 0))}",
        f"소계              {_fmt_krw(b.get('subtotal_krw', 0))}",
        "",
        f"(+) 매도미결제 국내 T+2  {_fmt_krw(pending_detail.get('domestic_krw', 0))}  ({pending_detail.get('domestic_count', 0)}건)",
        f"(+) 매도미결제 해외 T+1  {_fmt_krw(pending_detail.get('overseas_krw', 0))}  ({pending_detail.get('overseas_count', 0)}건)",
        f"(−) 미수매수 nrcvb_buy_amt  {_fmt_krw(liabilities.get('nrcvb_buy_amt_krw', 0))}",
        f"(−) 신용대출 tot_loan_amt    {_fmt_krw(liabilities.get('credit_loan_krw', 0))}",
        "",
        f"= 추정 총자산      {_fmt_krw(summary.get('total_net_worth_krw', 0))}",
        "",
        "— API 참조 (한투 앱 대조) —",
        f"output2.tot_evlu_amt (국내)  {_fmt_krw(kis_float(o2, 'tot_evlu_amt'))}",
        f"output2.nass_amt (순자산)    {_fmt_krw(kis_float(o2, 'nass_amt'))}",
        f"output2.nrcvb_buy_amt        {_fmt_krw(kis_float(o2, 'nrcvb_buy_amt'))}",
        f"output2.tot_loan_amt         {_fmt_krw(kis_float(o2, 'tot_loan_amt'))}",
        f"output2.tot_stln_slng_chgs   {_fmt_krw(kis_float(o2, 'tot_stln_slng_chgs'))}",
        f"output1 loan_amt 합(종목)    {_fmt_krw(loan_o1)}",
    ]

    for row in (pending_detail.get("domestic_trades") or [])[:5]:
        lines.append(
            f"  · 국내 미결제 {row.get('ticker')} {row.get('amount_krw'):,}원 결제일 {row.get('settlement_date')}"
        )
    for row in (pending_detail.get("overseas_trades") or [])[:5]:
        lines.append(
            f"  · 해외 미결제 {row.get('ticker')} {row.get('amount_krw'):,}원 결제일 {row.get('settlement_date')}"
        )
    extra_dom = len(pending_detail.get("domestic_trades") or []) - 5
    extra_ov = len(pending_detail.get("overseas_trades") or []) - 5
    if extra_dom > 0:
        lines.append(f"  … 국내 미결제 +{extra_dom}건")
    if extra_ov > 0:
        lines.append(f"  … 해외 미결제 +{extra_ov}건")

    return "\n".join(lines)


def fetch_balance_debug_snapshot(client) -> dict:
    """국내/해외 잔고 API raw 스냅샷."""
    snap: dict[str, Any] = {
        "domestic": {},
        "overseas_inquire": {},
        "meta": {},
        "comparison": {},
    }

    dom = client.fetch_domestic_inquire_balance_raw()
    snap["domestic"] = dom
    snap["meta"]["domestic_tr"] = dom.get("tr_id")
    snap["meta"]["domestic_rt_cd"] = dom.get("rt_cd")
    snap["meta"]["domestic_msg"] = dom.get("msg1")

    for exch in ("NASD", "NYSE", "AMEX"):
        try:
            data = client.fetch_overseas_inquire_balance_raw(exch)
            snap["overseas_inquire"][exch] = data
        except Exception as e:
            snap["overseas_inquire"][exch] = {"error": str(e)}

    try:
        snap["comparison"]["text"] = build_hts_comparison_section(client, snap)
    except Exception as e:
        snap["comparison"]["error"] = str(e)
        logger.warning("HTS 비교 섹션 생성 실패: %s", e)

    return snap


def build_debug_text_sections(snapshot: dict) -> list[str]:
    """로그/텔레그램용 텍스트 섹션 목록."""
    sections: list[str] = []
    meta = snapshot.get("meta") or {}
    sections.append(
        "🔧 <b>PeakExit /debug — KIS 잔고 raw</b>\n"
        f"국내 rt_cd={meta.get('domestic_rt_cd')} {meta.get('domestic_msg') or ''}"
    )

    cmp = snapshot.get("comparison") or {}
    if cmp.get("text"):
        sections.insert(1, cmp["text"])
    elif cmp.get("error"):
        sections.insert(1, f"⚠️ HTS 비교 섹션 오류: {cmp['error']}")

    dom = snapshot.get("domestic") or {}
    sections.append(
        _format_output2_block(
            f"1) 국내 inquire-balance output2 ({dom.get('tr_id', 'TTTC8434R')})",
            dom.get("output2"),
        )
    )
    o2 = dom.get("output2") or {}
    if isinstance(o2, dict) and o2:
        pick = {k: o2[k] for k in _DOMESTIC_OUTPUT2_KEYS if k in o2}
        if pick:
            sections.append(
                _format_output2_block("1b) 국내 output2 핵심(예수금·당일매매)", pick)
            )
    sections.append(
        _format_output1_rows(
            "3) 국내 inquire-balance output1 (종목별)",
            dom.get("output1") or [],
            _DOMESTIC_ROW_KEYS,
        )
    )

    ov = snapshot.get("overseas_inquire") or {}
    for exch in ("NASD", "NYSE", "AMEX"):
        block = ov.get(exch) or {}
        if block.get("error"):
            sections.append(f"{'=' * 40}\n2) 해외 inquire-balance {exch}\nERROR: {block['error']}")
            continue
        sections.append(
            _format_output2_block(
                f"2) 해외 inquire-balance output2 — {exch} ({block.get('tr_id', 'TTTS3012R')})",
                block.get("output2"),
            )
        )
        sections.append(
            _format_output1_rows(
                f"4) 해외 inquire-balance output1 — {exch} (종목별)",
                block.get("output1") or [],
                _OVERSEAS_ROW_KEYS,
            )
        )

    return sections


def chunk_sections_for_telegram(sections: list[str], max_len: int = _TELEGRAM_CHUNK) -> list[str]:
    """섹션을 텔레그램 메시지 크기로 분할."""
    chunks: list[str] = []
    current = ""
    for sec in sections:
        part = sec if not current else "\n\n" + sec
        if len(current) + len(part) <= max_len:
            current += part
        else:
            if current:
                chunks.append(current)
            if len(sec) <= max_len:
                current = sec
            else:
                # 긴 섹션은 줄 단위 분할
                lines = sec.split("\n")
                current = ""
                for line in lines:
                    add = line if not current else "\n" + line
                    if len(current) + len(add) > max_len:
                        chunks.append(current)
                        current = line
                    else:
                        current += add
    if current:
        chunks.append(current)
    return chunks


def log_balance_debug_snapshot(snapshot: dict) -> None:
    """서버 로그에 전체 raw 출력."""
    for sec in build_debug_text_sections(snapshot):
        logger.info("[balance_debug]\n%s", sec)
    logger.info(
        "[balance_debug] full JSON snapshot:\n%s",
        _json_pretty(snapshot),
    )


def telegram_messages_from_snapshot(snapshot: dict) -> list[str]:
    sections = build_debug_text_sections(snapshot)
    parts = chunk_sections_for_telegram(sections)
    total = len(parts)
    return [f"{body}\n\n<i>({i}/{total})</i>" for i, body in enumerate(parts, 1)]
