"""CLI разбора data/corpus/uploads/ — файлов, которые человек положил руками.

    python -m ingest.uploads

Drop-in: положил файл — ничего больше делать не нужно. Файлы не переименовываются (план,
раздел «Каталог пользовательских файлов») — идентичность несёт манифест (дедуп по path,
не по doc_id, см. ManifestStore.has_path), а не имя на диске.

Дата достаётся из имени файла (ingest/dates.py); не нашлась — publication_date: null,
needs_review: true, а не молчаливый пропуск: проектное правило — ни одного числа без даты
(knowledge/index-full.md). Сайдкар <имя файла>.meta.yaml переопределяет всё угаданное.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import yaml

from ingest.console import ensure_utf8_stdout
from ingest.dates import extract_date
from ingest.manifest import CORPUS_ROOT, ManifestStore
from ingest.naming import slugify

UPLOADS_DIR = CORPUS_ROOT / "uploads"
SIDECAR_SUFFIX = ".meta.yaml"

_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "html": "text/html",
    "htm": "text/html",
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "xml": "application/xml",
}


def _load_sidecar(file_path: Path) -> Dict[str, Any]:
    sidecar_path = file_path.with_name(file_path.name + SIDECAR_SUFFIX)
    if not sidecar_path.exists():
        return {}
    with open(sidecar_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def _iter_upload_files() -> Iterator[Path]:
    if not UPLOADS_DIR.exists():
        return
    for path in sorted(UPLOADS_DIR.rglob("*")):
        if path.is_file() and not path.name.endswith(SIDECAR_SUFFIX):
            yield path


def _resolve_publication_date(file_path: Path, sidecar: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Возвращает (publication_date ISO-строкой или None, откуда взята: 'sidecar'|'filename'|None)."""
    if sidecar.get("publication_date") is not None:
        return str(sidecar["publication_date"]), "sidecar"
    found = extract_date(file_path.stem)
    if found:
        return found[0].isoformat(), "filename"
    return None, None


def _build_entry(file_path: Path, relpath: str, sidecar: Dict[str, Any], manifest: ManifestStore, today: date) -> Dict[str, Any]:
    content = file_path.read_bytes()
    sha256 = hashlib.sha256(content).hexdigest()
    publication_date, date_source = _resolve_publication_date(file_path, sidecar)

    # doc_id всегда датирован: если дата выпуска не найдена, используем дату первого
    # обнаружения файла — она фиксируется один раз (has_path защищает от повторной
    # обработки на следующих прогонах), поэтому это не ломает идемпотентность.
    doc_id_date = publication_date or today.isoformat()
    slug = slugify(file_path.stem)[:60] or "file"
    doc_id = f"upload-{doc_id_date}-{slug}"
    if manifest.has_doc_id(doc_id):
        doc_id = f"{doc_id}-{sha256[:6]}"

    ext = file_path.suffix.lstrip(".").lower() or "bin"
    return {
        "doc_id": doc_id,
        "source_id": None,
        "origin": "uploaded",
        "publisher": sidecar.get("publisher"),
        "series": sidecar.get("series"),
        "edition_title": sidecar.get("edition_title"),
        "publication_date": publication_date,
        "segment": sidecar.get("segment"),
        "topics": sidecar.get("topics"),
        "geography": sidecar.get("geography"),
        "language": sidecar.get("language"),
        "landing": sidecar.get("landing"),
        "file_url": None,
        "path": relpath,
        "content_type": _CONTENT_TYPES.get(ext, "application/octet-stream"),
        "bytes": len(content),
        "sha256": sha256,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "access": sidecar.get("access"),
        "license": sidecar.get("license"),
        "supersedes": None,
        "needs_review": publication_date is None,
    }, date_source


def process_uploads(today: Optional[date] = None) -> Dict[str, int]:
    today = today or date.today()
    manifest = ManifestStore()
    counts = {"added": 0, "skipped": 0}

    for file_path in _iter_upload_files():
        relpath = file_path.relative_to(CORPUS_ROOT).as_posix()
        if manifest.has_path(relpath):
            counts["skipped"] += 1
            continue

        sidecar = _load_sidecar(file_path)
        entry, date_source = _build_entry(file_path, relpath, sidecar, manifest, today)
        manifest.append(entry)
        counts["added"] += 1
        tag = f" (дата из {date_source})" if date_source else " (дата не найдена — needs_review)"
        print(f"  OK {entry['doc_id']}  <-  {relpath}{tag}")

    return counts


def main() -> None:
    ensure_utf8_stdout()
    print(f"Обход {UPLOADS_DIR} ...")
    counts = process_uploads()
    print(f"\nДобавлено: {counts['added']}, пропущено (уже в манифесте): {counts['skipped']}")


if __name__ == "__main__":
    main()
