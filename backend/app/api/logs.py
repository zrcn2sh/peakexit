"""서버 로그 조회 API (일자별 파일)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from app.core.log_store import list_log_dates, read_logs_for_date

router = APIRouter()
SEOUL_TZ = ZoneInfo("Asia/Seoul")


@router.get("/dates")
def get_log_dates():
    """저장된 로그 일자 목록 (최신순)."""
    return {"dates": list_log_dates(), "today": datetime.now(SEOUL_TZ).date().isoformat()}


@router.get("/entries")
def get_logs(
    date_str: str = Query(..., alias="date", description="YYYY-MM-DD"),
    level: Optional[str] = Query(None, description="INFO|WARNING|ERROR|ALL"),
    q: Optional[str] = Query(None, description="본문 검색어"),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    newest_first: bool = Query(True),
):
    try:
        day = date.fromisoformat(date_str.strip()[:10])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"잘못된 날짜: {date_str}") from e
    return read_logs_for_date(
        day,
        level=level,
        q=q,
        limit=limit,
        offset=offset,
        newest_first=newest_first,
    )
