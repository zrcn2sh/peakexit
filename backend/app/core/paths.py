"""
데이터 디렉터리: Docker/Linux는 기본 /data, Windows 로컬은 backend/data.
환경변수 PEAKEXIT_DATA_DIR로 항상 덮어쓸 수 있음.
"""
from __future__ import annotations

import os
from pathlib import Path


def get_data_dir() -> Path:
    env = os.environ.get("PEAKEXIT_DATA_DIR", "").strip()
    if env:
        p = Path(env).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p.resolve()
    if os.name != "nt":
        return Path("/data")
    # Windows 로컬: backend/app/core/paths.py → backend/data
    root = Path(__file__).resolve().parents[2]
    d = (root / "data").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d
