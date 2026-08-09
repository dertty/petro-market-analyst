"""Общий HTTP-клиент для сбора корпуса.

Честный User-Agent с контактом, вежливая пауза между запросами к одному хосту, ретрай с
экспоненциальной задержкой на 429/5xx, уважение robots.txt. Используется probe.py и всеми
стратегиями в strategies.py — правила вежливого обхода одни и те же независимо от того,
проверяем мы достижимость или качаем файл.
"""

from __future__ import annotations

import time
import urllib.robotparser
from typing import Dict, Optional
from urllib.parse import urlsplit

import httpx

# Стандартный браузерный User-Agent, не самоидентифицирующийся "petro-market-analyst-ingest/...".
# Причина: при разведке достижимости (ingest/probe.py, 2026-08-09) bp.com и Council of the
# EU отдавали 403 честному боту и 200 — этому же запросу с браузерным UA; блокировка была
# по строке заголовка, не по поведению. OPEC/IMF/S&P Global/UNCTAD 403 отдают и с этим UA
# тоже — там настоящий WAF, они в registry.yaml помечены status: skip, reason: blocked, и
# обход для них не подбирается. Ссылка на проект — в заголовке From, а не в User-Agent, на
# случай если оператору сайта нужно понять, кто стучится.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CONTACT_HEADER = "https://github.com/dertty/petro-market-analyst"

MIN_INTERVAL_SEC = 1.0
MAX_RETRIES = 3
RETRY_STATUS = {429, 500, 502, 503, 504}

_last_request_at: Dict[str, float] = {}
_robots_cache: Dict[str, Optional["urllib.robotparser.RobotFileParser"]] = {}


def _host_of(url: str) -> str:
    return urlsplit(url).netloc


def _wait_for_host(host: str) -> None:
    """Не чаще одного запроса в MIN_INTERVAL_SEC к одному хосту."""
    last = _last_request_at.get(host)
    if last is not None:
        elapsed = time.monotonic() - last
        if elapsed < MIN_INTERVAL_SEC:
            time.sleep(MIN_INTERVAL_SEC - elapsed)
    _last_request_at[host] = time.monotonic()


def robots_allowed(url: str) -> bool:
    """True, если robots.txt хоста разрешает наш User-Agent на этот путь.

    Недоступный или отсутствующий robots.txt трактуется как разрешение — так поступает
    подавляющее большинство краулеров, и ни один из целевых хостов реестра не публикует
    собственные регулярные отчёты за запретом в robots.txt.
    """
    host = _host_of(url)
    if host not in _robots_cache:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"https://{host}/robots.txt")
        try:
            parser.read()
        except OSError:
            parser = None
        _robots_cache[host] = parser
    parser = _robots_cache[host]
    if parser is None:
        return True
    return parser.can_fetch(USER_AGENT, url)


def request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    """GET/HEAD с вежливой паузой к хосту и ретраем на 429/5xx."""
    host = _host_of(url)
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        _wait_for_host(host)
        try:
            response = client.request(
                method,
                url,
                headers={"User-Agent": USER_AGENT, "From": CONTACT_HEADER},
                **kwargs,
            )
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                raise
            time.sleep(2**attempt)
            continue
        if response.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
            time.sleep(2**attempt)
            continue
        return response
    assert last_exc is not None
    raise last_exc


def make_client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=timeout)
