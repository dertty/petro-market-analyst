"""Извлечение даты из URL или текста ссылки — общий помощник для listing/feed."""

from __future__ import annotations

import re
from datetime import date
from typing import Optional, Tuple

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# (?<!\d)/(?!\d) на каждом паттерне: без границ "20\d{2}" ловит случайные 4 цифры внутри
# произвольного длинного числа (например "2093" внутри file-ID "62093") — так уже ловилось
# на CBR-001 при разведке 2026-08-09.
_ISO_RE = re.compile(r"(?<!\d)(20\d{2})[-_/](\d{1,2})[-_/](\d{1,2})(?!\d)")
# Квантификаторы ограничены ({0,6}/{0,2}), а не *, чтобы регекс не мог свалиться в
# сверхлинейный backtracking на патологическом вводе.
#
# Границы — явный lookaround по буквам/цифрам, а не \b: в именах файлов "_" — обычный
# разделитель токенов ("IEA_OMR_July2026"), но для \b подчёркивание — "словесный" символ,
# и он его не считает границей. С \b "OMR_July2026" не матчился вовсе — падало в грубый
# фолбэк "только год" (see git history — поймано на тестовой загрузке в uploads/).
_MON_YEAR_RE = re.compile(
    r"(?<![A-Za-z0-9])(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"[a-z]{0,6}[\s_-]{0,2}(20\d{2}|\d{2})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_YEAR_MON_RE = re.compile(r"(?<!\d)(20\d{2})[-_](\d{1,2})(?!\d)")
# DDMMYYYY без разделителей, год в конце и заякорен — иначе неоднозначно с любой другой
# 8-значной последовательностью. Формат встречается у cbr.ru (report_10062026.pdf).
_DDMMYYYY_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)")
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


def _match_iso(m: re.Match) -> Optional[Tuple[date, str]]:
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    try:
        return date(y, mo, d), "day"
    except ValueError:
        return None


def _match_mon_year(m: re.Match) -> Optional[Tuple[date, str]]:
    mon = _MONTHS.get(m.group(1).lower()[:3])
    if not mon:
        return None
    yr_raw = m.group(2)
    yr = int(yr_raw) if len(yr_raw) == 4 else 2000 + int(yr_raw)
    return date(yr, mon, 1), "month"


def _match_year_mon(m: re.Match) -> Optional[Tuple[date, str]]:
    y, mo = int(m.group(1)), int(m.group(2))
    if not (1 <= mo <= 12):
        return None
    return date(y, mo, 1), "month"


def _match_ddmmyyyy(m: re.Match) -> Optional[Tuple[date, str]]:
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    try:
        return date(y, mo, d), "day"
    except ValueError:
        return None


def _match_year(m: re.Match) -> Optional[Tuple[date, str]]:
    return date(int(m.group(1)), 1, 1), "year"


# От самого точного паттерна к самому грубому — полная дата в строке не должна теряться
# за случайным совпадением одного года.
_PATTERNS = (
    (_ISO_RE, _match_iso),
    (_MON_YEAR_RE, _match_mon_year),
    (_YEAR_MON_RE, _match_year_mon),
    (_DDMMYYYY_RE, _match_ddmmyyyy),
    (_YEAR_RE, _match_year),
)


def _from_basename(text: str) -> str:
    """Хвост пути после последнего '/' — там дата чаще всего и живёт, а не в ID из пути."""
    return text.rsplit("/", 1)[-1]


def extract_date(text: str) -> Optional[Tuple[date, str]]:
    """Лучшее приближение: (дата, точность) где точность 'day'|'month'|'year', иначе None.

    Сначала пробуем хвост пути (basename) — там обычно и зашита дата выпуска, а не в
    служебном ID где-то раньше в URL; не нашли там — пробуем всю строку целиком.
    """
    if not text:
        return None
    for candidate in (_from_basename(text), text):
        for pattern, handler in _PATTERNS:
            m = pattern.search(candidate)
            if not m:
                continue
            result = handler(m)
            if result:
                return result
    return None
