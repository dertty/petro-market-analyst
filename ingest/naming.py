"""Правила именования файлов и doc_id — см. раздел «Раскладка и наименование» плана.

doc_id = <publisher_slug>-<series_slug>-<edition_key>[--<part>]. Для raw/ имя файла на
диске всегда равно doc_id + расширению: это инвариант, который проверяется отдельно
(ingest/collect.py --verify) и на который опирается будущий индексатор.
"""

from __future__ import annotations

import re
from typing import Optional

_CONTENT_TYPE_EXT = {
    "application/pdf": "pdf",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/csv": "csv",
    "application/xml": "xml",
    "text/xml": "xml",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Транслитерацию не делаем — только для латинских заголовков (заголовки событий на EN)."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug or "untitled"


def doc_id_for(publisher_slug: str, series_slug: str, edition_key: str, part: Optional[str] = None) -> str:
    doc_id = f"{publisher_slug}-{series_slug}-{edition_key}"
    if part:
        doc_id += f"--{part}"
    return doc_id


def raw_relpath(publisher_slug: str, series_slug: str, doc_id: str, ext: str) -> str:
    return f"raw/{publisher_slug}/{series_slug}/{doc_id}.{ext}"


def extension_for(content_type: str, fallback_url: str = "") -> str:
    """Расширение по фактическому Content-Type, не по хвосту URL — правило из плана.

    fallback_url используется только если сервер не прислал распознанный Content-Type
    (пустой ответ, text/plain-заглушка и т.п.) — тогда лучше хвост URL, чем ничего.
    """
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base in _CONTENT_TYPE_EXT:
        return _CONTENT_TYPE_EXT[base]
    tail = fallback_url.rsplit(".", 1)[-1].lower() if "." in fallback_url.rsplit("/", 1)[-1] else ""
    if tail and tail.isalnum() and len(tail) <= 5:
        return tail
    return "bin"
