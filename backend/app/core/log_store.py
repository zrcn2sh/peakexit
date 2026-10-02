"""
일자별 서버 로그 파일 (/data/logs/peakexit-YYYY-MM-DD.log).
대시보드 조회용 — docker logs와 별도로 영속 볼륨에 저장.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.paths import get_data_dir

SEOUL_TZ = ZoneInfo("Asia/Seoul")
LOG_RETENTION_DAYS = 30
_LOG_NAME_RE = re.compile(r"^peakexit-(\d{4}-\d{2}-\d{2})\.log$")
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+\|\s+"
    r"(?P<level>[A-Z]+)\s+\|\s+"
    r"(?P<logger>[^|]+)\|\s*"
    r"(?P<message>.*)$"
)

_configured = False


def get_logs_dir() -> Path:
    d = get_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path_for_date(day: date) -> Path:
    return get_logs_dir() / f"peakexit-{day.isoformat()}.log"


class _SeoulDailyFileHandler(logging.Handler):
    """레코드 시각(서울) 기준 일자 파일에 append."""

    def __init__(self, level: int = logging.INFO):
        super().__init__(level=level)
        self._current_day: Optional[date] = None
        self._stream = None

    def _ensure_stream(self, day: date) -> None:
        if self._current_day == day and self._stream is not None:
            return
        self._close_stream()
        path = log_path_for_date(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = open(path, "a", encoding="utf-8")
        self._current_day = day

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            created = datetime.fromtimestamp(record.created, tz=SEOUL_TZ)
            self._ensure_stream(created.date())
            msg = self.format(record)
            assert self._stream is not None
            self._stream.write(msg + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self._close_stream()
        super().close()


def setup_file_logging() -> None:
    """루트 로거에 일자별 파일 핸들러 1회 장착. stdout(uvicorn)은 유지."""
    global _configured
    if _configured:
        return

    get_logs_dir()
    handler = _SeoulDailyFileHandler(level=logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    root.addHandler(handler)

    # uvicorn access/error 도 파일에 남기기
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.INFO)

    _prune_old_logs()
    _configured = True
    logging.getLogger(__name__).info("일자별 서버 로그 파일 기록 시작 → %s", get_logs_dir())


def _prune_old_logs(keep_days: int = LOG_RETENTION_DAYS) -> None:
    cutoff = datetime.now(SEOUL_TZ).date() - timedelta(days=keep_days)
    for path in get_logs_dir().glob("peakexit-*.log"):
        m = _LOG_NAME_RE.match(path.name)
        if not m:
            continue
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if d < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def list_log_dates() -> list[str]:
    """사용 가능한 로그 일자 (최신순)."""
    days: list[str] = []
    for path in get_logs_dir().glob("peakexit-*.log"):
        m = _LOG_NAME_RE.match(path.name)
        if m:
            days.append(m.group(1))
    today = datetime.now(SEOUL_TZ).date().isoformat()
    if today not in days and log_path_for_date(datetime.now(SEOUL_TZ).date()).exists():
        days.append(today)
    # 오늘 파일이 아직 없어도 선택 가능하게 앞에 넣음
    if today not in days:
        days.append(today)
    days = sorted(set(days), reverse=True)
    return days


def _parse_line(raw: str) -> dict:
    line = raw.rstrip("\n")
    m = _LINE_RE.match(line)
    if not m:
        return {
            "ts": "",
            "level": "INFO",
            "logger": "",
            "message": line,
            "raw": line,
        }
    return {
        "ts": m.group("ts"),
        "level": m.group("level").strip(),
        "logger": m.group("logger").strip(),
        "message": m.group("message"),
        "raw": line,
    }


def read_logs_for_date(
    day: date,
    *,
    level: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
    newest_first: bool = True,
) -> dict:
    path = log_path_for_date(day)
    level_u = (level or "").strip().upper()
    query = (q or "").strip().lower()
    limit = max(1, min(int(limit or 500), 2000))
    offset = max(0, int(offset or 0))

    if not path.exists():
        return {
            "date": day.isoformat(),
            "exists": False,
            "total": 0,
            "offset": offset,
            "limit": limit,
            "entries": [],
        }

    matched: list[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            if not raw.strip():
                continue
            entry = _parse_line(raw)
            if level_u and level_u != "ALL":
                if entry["level"].upper() != level_u:
                    continue
            if query and query not in entry["raw"].lower():
                continue
            matched.append(entry)

    total = len(matched)
    if newest_first:
        matched.reverse()
    page = matched[offset : offset + limit]
    return {
        "date": day.isoformat(),
        "exists": True,
        "total": total,
        "offset": offset,
        "limit": limit,
        "newest_first": newest_first,
        "entries": page,
    }
