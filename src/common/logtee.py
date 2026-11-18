"""
logtee.py
~~~~~~~~~
Mirror everything written to stdout/stderr (i.e. all existing print() calls)
into a log file, without modifying any print() call site.

Usage (once, at the top of an entry point):
    from common.logtee import enable_log_tee
    enable_log_tee()          # appends to <project root>/log.txt
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

DEFAULT_LOG_PATH = Path(__file__).resolve().parents[2] / "log.txt"
_lock = threading.Lock()
_installed = False


class _Tee:
    def __init__(self, stream, log_file):
        self._stream = stream
        self._log = log_file

    def write(self, data):
        n = self._stream.write(data)
        try:
            with _lock:
                self._log.write(data)
                self._log.flush()
        except Exception:
            pass
        return n

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def enable_log_tee(path: Path | str = DEFAULT_LOG_PATH) -> None:
    """Duplicate stdout and stderr into `path` (append mode, UTF-8)."""
    global _installed
    if _installed:
        return
    log_file = open(path, "a", encoding="utf-8", errors="replace")
    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)
    _installed = True
