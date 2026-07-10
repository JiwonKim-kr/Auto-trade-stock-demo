"""JSON 로깅(§3.8) — Cloud Logging severity 필드 + 예외 직렬화."""

from __future__ import annotations

import json
import logging

from app.core.logging_setup import JsonFormatter


def _record(level: int, msg: str, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(name="app.tick", level=level, pathname=__file__, lineno=1,
                             msg=msg, args=(), exc_info=exc_info)


def test_json_formatter_fields():
    out = json.loads(JsonFormatter().format(_record(logging.WARNING, "레짐 STRESS — 신규 중단")))
    assert out["severity"] == "WARNING"
    assert out["logger"] == "app.tick"
    assert out["message"] == "레짐 STRESS — 신규 중단"
    assert out["ts"].endswith("+00:00")          # UTC


def test_json_formatter_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        rec = _record(logging.ERROR, "틱 실패", exc_info=sys.exc_info())
    out = json.loads(JsonFormatter().format(rec))
    assert out["severity"] == "ERROR" and "ValueError: boom" in out["exception"]
