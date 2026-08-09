"""Четыре способа найти файлы выпусков — см. план, раздел «Движок: четыре стратегии».

Каждая функция iter_* — генератор Candidate: что скачать и под каким edition_key
записать. Загрузка, проверка байт и запись в манифест — в collect.py; здесь только
обнаружение.

listing и feed — best-effort. Общий алгоритм (собрать ссылки на документы, вытащить дату
из URL или текста, отсортировать) работает не для каждого сайта одинаково хорошо: часть
издателей публикует не прямую ссылку на файл, а промежуточную HTML-страницу выпуска.
Источники, где это не сработало, collect.py репортует как failed по каждому source — по
правилу «отказ источника — громкая ошибка», а не молча пропускает.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ingest import http
from ingest.dates import extract_date
from ingest.naming import slugify

DOC_EXTENSIONS = {"pdf", "xlsx", "xls", "csv", "doc", "docx"}


@dataclass
class Candidate:
    edition_key: Optional[str]  # None => присваивает движок (стратегия single)
    file_url: str
    publication_date: Optional[str]  # ISO-строка или None
    part: Optional[str] = None


def _prev_month(d: date) -> date:
    if d.month == 1:
        return date(d.year - 1, 12, 1)
    return date(d.year, d.month - 1, 1)


def iter_date_pattern(source: Dict[str, Any], today: date) -> Iterator[Candidate]:
    params = source["params"]
    depth = source["depth"]
    step = params["step"]

    if step == "month":
        d = date(today.year, today.month, 1)
        dates = []
        for _ in range(depth["months"]):
            dates.append(d)
            d = _prev_month(d)
    elif step == "week":
        weekday = params.get("weekday", 2)  # среда по умолчанию (WPSR)
        delta = (today.weekday() - weekday) % 7
        anchor = today - timedelta(days=delta)
        weeks = max(1, round(depth["months"] * 4.348))
        dates = [anchor - timedelta(weeks=i) for i in range(weeks)]
    else:
        raise ValueError(f"{source['id']}: неизвестный params.step={step!r}")

    url_template = params["url"]
    for d in dates:
        url = url_template.format(d=d)
        if params.get("lowercase"):
            url = url.lower()
        edition_key = d.strftime("%Y-%m") if step == "month" else d.strftime("%Y-%m-%d")
        yield Candidate(edition_key=edition_key, file_url=url, publication_date=d.isoformat())


def _resolve_links(client, landing_url: str) -> List[Dict[str, str]]:
    response = http.request(client, "GET", landing_url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    seen_urls = set()
    items: List[Dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        tail = href.rsplit("/", 1)[-1]
        ext = tail.rsplit(".", 1)[-1].lower().split("?", 1)[0] if "." in tail else ""
        if ext not in DOC_EXTENSIONS:
            continue
        url = urljoin(landing_url, href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        # title часто несёт дату/заголовок, когда видимый текст ссылки — просто "PDF"
        # (иконка вместо подписи); например EIA MER: title="October 2026".
        text = " ".join(filter(None, [a.get_text(" ", strip=True), a.get("title", "")]))
        items.append({"url": url, "text": text})
    return items


def _edition_key(d: date, precision: str, annual: bool) -> str:
    if annual:
        # Годовая глубина считается в изданиях (годах), а не в датах публикации: у одного
        # выпуска нередко несколько файлов (narrative + companion-документы) — все они
        # должны схлопнуться в один edition_key, а не растащить лимит depth.editions.
        return str(d.year)
    if precision == "day":
        return d.strftime("%Y-%m-%d")
    if precision == "year":
        return str(d.year)
    return d.strftime("%Y-%m")


def iter_listing(source: Dict[str, Any], client, today: date) -> Iterator[Candidate]:
    landing = source["params"]["landing"]
    depth = source["depth"]
    annual = "editions" in depth

    dated_items = []
    for item in _resolve_links(client, landing):
        found = extract_date(item["url"]) or extract_date(item["text"])
        if not found:
            continue
        d, precision = found
        dated_items.append((d, _edition_key(d, precision, annual), item))
    dated_items.sort(key=lambda t: t[0], reverse=True)

    if annual:
        # N последних РАЗЛИЧНЫХ edition_key, а не N любых пунктов — иначе несколько файлов
        # на одну редакцию съедают лимит и реальных лет остаётся меньше depth.editions.
        ordered_keys: List[str] = []
        for _d, key, _item in dated_items:
            if key not in ordered_keys:
                ordered_keys.append(key)
        allowed = set(ordered_keys[: depth["editions"]])
        selected = [t for t in dated_items if t[1] in allowed]
    else:
        cutoff = today - timedelta(days=depth["months"] * 31)
        selected = [t for t in dated_items if t[0] >= cutoff]

    seen_keys = set()
    for d, edition_key, item in selected:
        part = None
        if edition_key in seen_keys:
            part = hashlib.sha1(item["url"].encode()).hexdigest()[:6]
        seen_keys.add(edition_key)
        yield Candidate(edition_key=edition_key, file_url=item["url"], publication_date=d.isoformat(), part=part)


def iter_feed(source: Dict[str, Any], client, today: date) -> Iterator[Candidate]:
    landing = source["params"]["landing"]
    depth = source["depth"]
    cutoff = today - timedelta(days=depth["months"] * 31)

    for item in _resolve_links(client, landing):
        found = extract_date(item["url"]) or extract_date(item["text"])
        if not found:
            continue
        d, _precision = found
        if d < cutoff:
            continue
        slug = slugify(item["text"])[:60] or "item"
        edition_key = f"{d.isoformat()}-{slug}"
        yield Candidate(edition_key=edition_key, file_url=item["url"], publication_date=d.isoformat())


def iter_single(source: Dict[str, Any]) -> Iterator[Candidate]:
    params = source["params"]
    file_url = params.get("file_url") or params.get("landing") or source["landing"]
    yield Candidate(edition_key=None, file_url=file_url, publication_date=None)


def discover(source: Dict[str, Any], client, today: date) -> Iterator[Candidate]:
    strategy = source["strategy"]
    if strategy == "date_pattern":
        return iter_date_pattern(source, today)
    if strategy == "listing":
        return iter_listing(source, client, today)
    if strategy == "feed":
        return iter_feed(source, client, today)
    if strategy == "single":
        return iter_single(source)
    raise ValueError(f"{source['id']}: неизвестная strategy={strategy!r}")
