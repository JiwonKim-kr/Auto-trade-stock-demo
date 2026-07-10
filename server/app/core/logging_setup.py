"""JSON 구조화 로깅 (PLAN §3.8) — Cloud Logging 이 severity 를 집도록 stdout 한 줄 JSON.

의존성 없이 stdlib Formatter 만 교체. LOG_FORMAT=json 일 때만 활성(로컬 기본 text 무변경).
uvicorn 로거는 핸들러를 비우고 root 로 전파시켜 포맷을 일원화한다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "severity": record.levelname,       # Cloud Logging 표준 필드명
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_json_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    if root.level in (logging.NOTSET, logging.WARNING):   # 명시 설정 없으면 INFO
        root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
