"""CLI автосбора корпуса. Один прогон одной командой:

    python -m ingest.collect                    # все source со status: collect
    python -m ingest.collect --only EIA-001
    python -m ingest.collect --dry-run           # план без единой загрузки

Идемпотентно: для детерминированных стратегий (date_pattern/listing/feed) повторный
прогон не делает ни одного HTTP-запроса на уже собранные edition_key — проверка по
doc_id в манифесте. Для single повторный прогон качает документ и сравнивает sha256 с
последней известной версией; при совпадении файл не остаётся на диске и манифест не
пополняется.

Отказ источника — громкая ошибка (правило из плана): если стратегия не нашла ни одного
выпуска или ни один кандидат не скачался, источник попадает в итоговую таблицу как
failed, а не пропускается молча.
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ingest import http, strategies
from ingest.console import ensure_utf8_stdout
from ingest.manifest import CORPUS_ROOT, ManifestStore
from ingest.naming import doc_id_for, extension_for, raw_relpath
from ingest.registry import collect_sources

RAW_ROOT = CORPUS_ROOT / "raw"
UPLOADS_ROOT = CORPUS_ROOT / "uploads"

MIN_BYTES = 50 * 1024
PDF_MAGIC = b"%PDF"


def _validate_bytes(content: bytes, content_type: str) -> Optional[str]:
    """None, если содержимое похоже на настоящий документ; иначе причина отказа."""
    if len(content) < MIN_BYTES:
        return f"файл меньше {MIN_BYTES} байт ({len(content)}) — похоже на заглушку/страницу отказа"
    base_ct = (content_type or "").split(";", 1)[0].strip().lower()
    if base_ct == "application/pdf" and not content.startswith(PDF_MAGIC):
        return "Content-Type application/pdf, но файл не начинается с %PDF"
    return None


def _fetch_and_save(
    client,
    source: Dict[str, Any],
    doc_id: str,
    edition_key: str,
    publication_date: Optional[str],
    file_url: str,
    supersedes: Optional[str],
) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """Возвращает (result, entry, error) где result — 'downloaded'|'failed'."""
    try:
        response = http.request(client, "GET", file_url)
        response.raise_for_status()
    # Любая сетевая ошибка репортится в таблицу, а не роняет весь прогон.
    except Exception as exc:  # noqa: BLE001
        return "failed", None, f"{type(exc).__name__}: {exc}"

    content = response.content
    content_type = response.headers.get("content-type", "")
    problem = _validate_bytes(content, content_type)
    if problem:
        return "failed", None, problem

    ext = extension_for(content_type, file_url)
    relpath = raw_relpath(source["publisher_slug"], source["series_slug"], doc_id, ext)
    target = CORPUS_ROOT / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)

    entry = {
        "doc_id": doc_id,
        "source_id": source["id"],
        "origin": "collected",
        "publisher": source["publisher"],
        "series": source["series"],
        "edition_key": edition_key,
        "publication_date": publication_date,
        "frequency": source.get("frequency"),
        "segment": source.get("segment"),
        "landing": source.get("landing"),
        "file_url": file_url,
        "path": relpath,
        "content_type": content_type,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "access": source.get("access"),
        "license": source.get("license"),
        "supersedes": supersedes,
        "needs_review": False,
    }
    return "downloaded", entry, None


def _process_candidate(
    source: Dict[str, Any],
    candidate: "strategies.Candidate",
    manifest: ManifestStore,
    client,
    dry_run: bool,
) -> str:
    publisher_slug = source["publisher_slug"]
    series_slug = source["series_slug"]

    if candidate.edition_key is not None:
        doc_id = doc_id_for(publisher_slug, series_slug, candidate.edition_key, candidate.part)
        if manifest.has_doc_id(doc_id):
            return "skipped"
        if dry_run:
            print(f"  [would fetch] {doc_id}  <-  {candidate.file_url}")
            return "skipped"
        result, entry, error = _fetch_and_save(
            client, source, doc_id, candidate.edition_key, candidate.publication_date,
            candidate.file_url, supersedes=None,
        )
        if result == "failed":
            print(f"  FAILED {doc_id}: {error}")
            return "failed"
        manifest.append(entry)
        print(f"  OK {doc_id}  ({entry['bytes']} bytes)")
        return "downloaded"

    # candidate.edition_key is None -> стратегия single: качаем и сверяем по sha256.
    if dry_run:
        print(f"  [would check] {source['id']}  <-  {candidate.file_url}")
        return "skipped"
    previous = manifest.latest_for_source(source["id"])
    today_key = date.today().isoformat()
    doc_id = doc_id_for(publisher_slug, series_slug, today_key)
    if manifest.has_doc_id(doc_id):
        return "skipped"  # уже проверяли сегодня

    result, entry, error = _fetch_and_save(
        client, source, doc_id, today_key, today_key, candidate.file_url,
        supersedes=previous["doc_id"] if previous else None,
    )
    if result == "failed":
        print(f"  FAILED {source['id']}: {error}")
        return "failed"
    if previous is not None and previous["sha256"] == entry["sha256"]:
        (CORPUS_ROOT / entry["path"]).unlink()
        print(f"  UNCHANGED {source['id']} (sha256 совпадает с {previous['doc_id']})")
        return "skipped"
    manifest.append(entry)
    tag = " [новая версия]" if previous else " [первая версия]"
    print(f"  OK {doc_id}  ({entry['bytes']} bytes){tag}")
    return "downloaded"


def run(only: Optional[List[str]], dry_run: bool) -> Dict[str, Dict[str, int]]:
    sources = collect_sources()
    if only:
        wanted = set(only)
        sources = [s for s in sources if s["id"] in wanted]

    manifest = ManifestStore()
    today = date.today()
    totals: Dict[str, Dict[str, int]] = {}

    with http.make_client() as client:
        for source in sources:
            print(f"\n== {source['id']} — {source['publisher']} / {source['series']} ({source['strategy']}) ==")
            try:
                candidates = list(strategies.discover(source, client, today))
            except Exception as exc:  # noqa: BLE001
                print(f"  FAILED (discovery): {type(exc).__name__}: {exc}")
                totals[source["id"]] = {"downloaded": 0, "skipped": 0, "failed": 1}
                continue
            if not candidates:
                print("  FAILED: стратегия не нашла ни одного выпуска")
                totals[source["id"]] = {"downloaded": 0, "skipped": 0, "failed": 1}
                continue

            counts = {"downloaded": 0, "skipped": 0, "failed": 0}
            for candidate in candidates:
                result = _process_candidate(source, candidate, manifest, client, dry_run)
                counts[result] += 1
            totals[source["id"]] = counts

    return totals


def _print_summary(totals: Dict[str, Dict[str, int]]) -> None:
    print("\n" + "=" * 60)
    print(f"{'source':10} {'downloaded':>10} {'skipped':>8} {'failed':>7}")
    grand = {"downloaded": 0, "skipped": 0, "failed": 0}
    dead_sources = []      # 0 успехов вообще — "громкая ошибка" из плана
    partial_sources = []   # есть и успехи, и отказы — тоже стоит показать, но не как dead
    for source_id, counts in totals.items():
        print(f"{source_id:10} {counts['downloaded']:>10} {counts['skipped']:>8} {counts['failed']:>7}")
        for key in grand:
            grand[key] += counts[key]
        if counts["failed"] and not counts["downloaded"] and not counts["skipped"]:
            dead_sources.append(source_id)
        elif counts["failed"]:
            partial_sources.append(source_id)
    print("-" * 60)
    print(f"{'TOTAL':10} {grand['downloaded']:>10} {grand['skipped']:>8} {grand['failed']:>7}")
    if dead_sources:
        print(f"\nБез единого успешно обработанного выпуска (нужно чинить): {dead_sources}")
    if partial_sources:
        print(f"\nЧасть выпусков не скачалась, но источник в целом работает: {partial_sources}")


def _files_on_disk() -> set:
    found = set()
    for root in (RAW_ROOT, UPLOADS_ROOT):
        if root.exists():
            found |= {p.relative_to(CORPUS_ROOT).as_posix() for p in root.rglob("*") if p.is_file()}
    # Сайдкары uploads/ — не документы, в манифесте им отдельной строки не полагается.
    return {p for p in found if not p.endswith(".meta.yaml")}


def _diff_problems(on_disk: set, in_manifest: set) -> List[str]:
    orphans = sorted(on_disk - in_manifest)
    missing = sorted(in_manifest - on_disk)
    problems = []
    if orphans:
        problems.append(f"{len(orphans)} файл(ов) на диске без строки в манифесте: {orphans[:5]}{' …' if len(orphans) > 5 else ''}")
    if missing:
        problems.append(f"{len(missing)} строк(и) манифеста без файла на диске: {missing[:5]}{' …' if len(missing) > 5 else ''}")
    return problems


def _check_entry(entry: Dict[str, Any]) -> List[str]:
    """Инвариант "имя файла = doc_id" и PDF-магия — только для raw/ (origin=collected):
    у uploads/ имя файла намеренно не совпадает с doc_id (файлы пользователя не переименовываются)."""
    if entry.get("origin") != "collected":
        return []
    target = CORPUS_ROOT / entry["path"]
    problems = []
    if target.stem != entry["doc_id"]:
        problems.append(f"{entry['path']}: имя файла ({target.stem}) != doc_id ({entry['doc_id']})")
    if not target.exists():
        return problems  # отсутствие уже отмечено в _diff_problems
    content_type = (entry.get("content_type") or "").split(";", 1)[0].strip().lower()
    if content_type == "application/pdf":
        with open(target, "rb") as f:
            head = f.read(4)
        if head != PDF_MAGIC:
            problems.append(f"{entry['path']}: Content-Type application/pdf, но файл не начинается с %PDF")
    return problems


def verify_corpus() -> bool:
    """Проверяет инварианты из плана: манифест = файлы на диске, raw/-имя = doc_id,
    PDF начинается с %PDF. Возвращает True, если расхождений не найдено.

    Не то же самое, что валидация при скачивании (_validate_bytes) — та ловит проблему в
    момент загрузки; это — независимая сверка уже лежащего на диске корпуса с манифестом,
    которая поймает и ручное вмешательство в data/corpus/ в обход collect.py.
    """
    manifest = ManifestStore()
    on_disk = _files_on_disk()
    in_manifest = {e["path"] for e in manifest.entries}

    problems = _diff_problems(on_disk, in_manifest)
    for entry in manifest.entries:
        problems.extend(_check_entry(entry))

    total_bytes = sum((CORPUS_ROOT / p).stat().st_size for p in on_disk if (CORPUS_ROOT / p).exists())
    print(f"Файлов на диске: {len(on_disk)}; строк в манифесте: {len(manifest)}; "
          f"размер data/corpus: {total_bytes / (1024 * 1024):.1f} МБ")

    if problems:
        print(f"\nНайдено расхождений: {len(problems)}")
        for p in problems:
            print(f"  - {p}")
        return False
    print("Расхождений не найдено.")
    return True


def main() -> None:
    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", metavar="SOURCE_ID", help="собрать только эти id из реестра")
    parser.add_argument("--dry-run", action="store_true", help="только показать план, ничего не качать")
    parser.add_argument("--verify", action="store_true", help="только сверить манифест с диском, ничего не качать")
    args = parser.parse_args()

    if args.verify:
        ok = verify_corpus()
        raise SystemExit(0 if ok else 1)

    totals = run(args.only, args.dry_run)
    _print_summary(totals)


if __name__ == "__main__":
    main()
