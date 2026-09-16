"""Structured logging: one JSON object per line on stdout, plus a rotating file."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Optional, TextIO

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}


class StructuredLogger(object):
    def __init__(self, level: str = "info", stream: Optional[TextIO] = None,
                 file_path: Optional[str] = None, human: bool = True):
        self.threshold = _LEVELS.get(level, 20)
        self.stream = stream if stream is not None else sys.stdout
        self.human = human
        self._lock = threading.Lock()
        self._fh = None
        if file_path:
            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
            self._fh = open(file_path, "a", encoding="utf-8")

    def _emit(self, level: str, event: str, **fields: Any) -> None:
        if _LEVELS[level] < self.threshold:
            return
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "level": level, "event": event}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            if self.human:
                extras = " ".join(
                    "%s=%s" % (k, v) for k, v in fields.items() if v is not None)
                self.stream.write("%-8s %-26s %s\n" % (level.upper(), event, extras))
            else:
                self.stream.write(line + "\n")
            self.stream.flush()
            if self._fh is not None:
                self._fh.write(line + "\n")
                self._fh.flush()

    def debug(self, event: str, **fields: Any) -> None:
        self._emit("debug", event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit("info", event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit("warning", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("error", event, **fields)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
