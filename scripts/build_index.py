#!/usr/bin/env python3
"""Сборка векторного индекса по отчётам из reports/pdf/.

    docker compose exec ouroboros python /workspace/scripts/build_index.py

Запускается в контейнере рантайма: там уже стоят pypdf, chromadb и fastembed, и туда
же при первом запуске скачиваются веса эмбеддера (в data/models, каталог переживает
пересборку образа).

Индексация всегда полная. Корпус небольшой, а инкрементальное обновление потребовало бы
сверки содержимого файлов с индексом — сложность, которая тут не окупается.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = PROJECT_ROOT / "skills" / "petro_rag"
sys.path.insert(0, str(SKILL_DIR))

from lib import store  # noqa: E402
from lib.chunking import Chunk, chunk_document  # noqa: E402
from lib.embedding import make_embedder  # noqa: E402

PDF_DIR = PROJECT_ROOT / "reports" / "pdf"
MANIFEST = PROJECT_ROOT / "reports" / "manifest.yaml"


def _load_manifest() -> Dict[str, dict]:
    if not MANIFEST.exists():
        return {}
    import yaml

    with open(MANIFEST, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return {str(k): (v or {}) for k, v in data.items()}


def _guess_date(stem: str) -> str:
    """Дата из имени файла. Переиспользует разбор из подсистемы сбора корпуса.

    Совпадения по одному лишь году отбрасываются. В именах вроде
    «neftyanoi-rinok-v-2026-godu» год — это период, О КОТОРОМ отчёт, а не дата выпуска,
    и extract_date честно вернул бы 1 января. Такая дата в ссылке на источник хуже
    отсутствующей: выглядит точной и не является таковой.
    """
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from ingest.dates import extract_date
    except Exception:
        return ""
    found = extract_date(stem)
    if not found or found[1] == "year":
        return ""
    return found[0].isoformat()


def _meta_for(pdf: Path, manifest: Dict[str, dict]) -> Dict[str, str]:
    entry = manifest.get(pdf.name)
    known = entry is not None
    entry = entry or {}

    report = str(entry.get("report") or "").strip()
    if not report:
        # Слаг в имени файла — единственное, что есть без записи в манифесте.
        report = pdf.stem.replace("-", " ").replace("_", " ").strip().capitalize()

    # Запись в манифесте — источник истины: пустое поле date там означает «дата
    # неизвестна», и угадывать вопреки этому нельзя. Догадка допустима только для
    # файлов, которых в манифесте нет вовсе.
    if known and "date" in entry:
        date = str(entry.get("date") or "").strip()
    else:
        date = str(entry.get("date") or "").strip() or _guess_date(pdf.stem)

    return {
        "report": report,
        "publisher": str(entry.get("publisher") or "").strip(),
        "date": date,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedder", default=None, help="Например openai:text-embedding-3-small")
    parser.add_argument("--index", default=None, help="Каталог индекса")
    parser.add_argument("--dry-run", action="store_true", help="Только нарезка, без векторов")
    args = parser.parse_args()

    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"В {PDF_DIR} нет ни одного PDF — индексировать нечего.", file=sys.stderr)
        return 1

    manifest = _load_manifest()
    chunks: List[Chunk] = []
    print(f"Отчётов: {len(pdfs)}\n")
    for pdf in pdfs:
        meta = _meta_for(pdf, manifest)
        produced = chunk_document(
            pdf, report=meta["report"], publisher=meta["publisher"], date=meta["date"]
        )
        chunks.extend(produced)
        pages = len({chunk.page for chunk in produced})
        date_note = meta["date"] or "дата неизвестна"
        print(f"  {pdf.name}\n    {meta['report']} ({date_note})")
        print(f"    страниц с текстом: {pages}, чанков: {len(produced)}")

    print(f"\nВсего чанков: {len(chunks)}")
    if args.dry_run:
        print("--dry-run: векторы не считались, индекс не тронут.")
        return 0

    if not chunks:
        print("Ни одного чанка — индекс не собран.", file=sys.stderr)
        return 1

    embedder = make_embedder(args.embedder)
    print(f"\nЭмбеддер: {embedder.id} (dim {embedder.dim})")
    print("Первый запуск скачивает веса модели — это долго, дальше они берутся с диска.")

    started = time.monotonic()
    written = store.build(chunks, embedder, path=args.index)
    elapsed = time.monotonic() - started

    print(f"Записано чанков: {written} за {elapsed:.1f} с")
    print(f"Индекс: {store.index_path(args.index)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
