"""UTF-8 для stdout/stderr при запуске на Windows.

Консоль Windows по умолчанию использует cp1252/cp866, а в отчётах ingest — кириллица.
Без этого print() падает на первом же русском слове с UnicodeEncodeError. Вызывается первой
строкой в main() каждого CLI-скрипта ingest.
"""

from __future__ import annotations

import sys


def ensure_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
