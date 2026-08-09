#!/usr/bin/env python3
"""Проверка и подбор якорей для eval-набора.

    docker compose exec ouroboros python /workspace/scripts/check_anchors.py
    docker compose exec ouroboros python /workspace/scripts/check_anchors.py --suggest

Якорь — дословный фрагмент отчёта, по которому eval засчитывает попадание независимо
от номера страницы. Смысл в том, что при смене размера чанка границы страниц плавают,
а формулировка с числом — нет.

Якорь, который не совпадает с текстом посимвольно, молча обнуляет anchor-метрику и
выглядит как «поиск стал хуже». Поэтому здесь два режима: проверить уже проставленные
(по умолчанию) и предложить кандидатов из relevant_text (--suggest).

Сверка идёт по тексту ЧАНКОВ, а не страниц: поиск работает с чанками, а нарезка
выбрасывает куски короче MIN_PAGE_CHARS // 2. Якорь, который есть на странице, но не
попал ни в один чанк, для поиска недостижим — проверка по странице объявила бы его
исправным, и метрика молча упёрлась бы в потолок, неотличимый от плохого ранжирования.

Заодно проверяется поле facts — знаменатель метрики полноты: его страницы обязаны
входить в pages. Иначе знаменатель полноты можно было бы расширить, не тронув
знаменатель Recall, то есть подогнать эталон под выдачу незаметно.

Сравнение мягкое по пробелам и регистру: в PDF щедро рассыпаны неразрывные пробелы,
и требовать их точного совпадения от человека, размечающего набор, бессмысленно.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = PROJECT_ROOT / "skills" / "petro_rag"
sys.path.insert(0, str(SKILL_DIR))

from lib.chunking import chunk_document  # noqa: E402

GOLDEN = PROJECT_ROOT / "reports" / "golden_dataset.jsonl"
PDF_DIR = PROJECT_ROOT / "reports" / "pdf"

_SPACES = re.compile(r"\s+")

# Кандидаты в якоря: число с единицей измерения. Именно они переживают перенарезку и
# однозначно идентифицируют фрагмент.
_NUMERIC = re.compile(
    r"\$?\s?\d[\d\s.,–—-]*\s*"
    r"(?:\$?/барр\.?|долл\.?/барр\.?|%|п\.\s?п\.|трлн\s+руб\.?|млрд|млн\s+барр\.?|млн)",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return _SPACES.sub(" ", text.replace(" ", " ").replace(" ", " ")).strip().lower()


def _page_chunks(document: str) -> Dict[int, List[str]]:
    """Нормализованные тексты чанков документа, сгруппированные по странице."""
    path = PDF_DIR / document
    if not path.exists():
        return {}
    grouped: Dict[int, List[str]] = {}
    for chunk in chunk_document(path, report=""):
        grouped.setdefault(chunk.page, []).append(_normalize(chunk.text))
    return grouped


def _present_in(text: str, chunks: Sequence[str]) -> bool:
    """Есть ли фрагмент целиком хотя бы в одном чанке.

    Именно «в одном», а не в склейке: чанки соседних страниц идут подряд, и на шве
    склейки возникает текст, которого в отчёте нет.
    """
    needle = _normalize(text)
    return any(needle in chunk for chunk in chunks)


def _pages_of(row: dict) -> List[int]:
    if "pages" in row:
        return [int(p) for p in row["pages"]]
    if "page" in row:
        return [int(row["page"])]
    return []


def _candidates(row: dict, chunks: Sequence[str]) -> List[str]:
    """Фрагменты из relevant_text, дословно присутствующие в чанках."""
    relevant = str(row.get("relevant_text") or "")

    found: List[str] = []
    for match in _NUMERIC.finditer(relevant):
        candidate = match.group(0).strip(" ,;")
        if len(candidate) < 2:
            continue
        if _present_in(candidate, chunks) and candidate not in found:
            found.append(candidate)

    # Если чисел нет (качественные вопросы), берём самую длинную дословную фразу.
    if not found:
        for sentence in re.split(r"[.;]", relevant):
            words = sentence.split()
            for size in (6, 5, 4):
                for start in range(0, max(len(words) - size + 1, 0)):
                    phrase = " ".join(words[start : start + size])
                    if len(phrase) > 15 and _present_in(phrase, chunks):
                        found.append(phrase)
                        break
                if found:
                    break
            if found:
                break
    return found[:3]


def _fact_problems(row: dict, chunks_by_page: Dict[int, List[str]]) -> List[str]:
    """Нарушения контракта facts. Пустой список — поле в порядке или отсутствует."""
    declared = set(_pages_of(row))
    problems: List[str] = []
    for position, fact in enumerate(row.get("facts") or [], start=1):
        where = f"facts[{position}]"
        if not isinstance(fact, dict):
            problems.append(f"{where}: не объект")
            continue
        page = fact.get("page")
        anchor = str(fact.get("anchor") or "")
        if not str(fact.get("why") or "").strip():
            problems.append(f"{where}: пустой why — знаменатель полноты должен быть обоснован")
        if page not in declared:
            # Инвариант, а не придирка: расширить знаменатель полноты, не тронув pages,
            # значит поднять требования к поиску мимо метрики, которая на виду.
            problems.append(f"{where}: страница {page} не входит в pages {sorted(declared)}")
        elif not anchor:
            problems.append(f"{where}: пустой anchor")
        elif not _present_in(anchor, chunks_by_page.get(int(page), [])):
            problems.append(f"{where}: якорь не найден в чанках стр. {page}: {anchor!r}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suggest", action="store_true", help="Предложить якоря и вывести JSONL")
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in GOLDEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    cache: Dict[str, Dict[int, List[str]]] = {}
    problems = 0
    updated: List[dict] = []

    for row in rows:
        if str(row.get("expected") or "").lower() == "none":
            updated.append(row)
            continue

        document = str(row.get("document") or "")
        if document not in cache:
            cache[document] = _page_chunks(document)
        chunks_by_page = cache[document]

        chunks = [text for number in _pages_of(row) for text in chunks_by_page.get(number, [])]
        if not chunks:
            print(f"[{row['id']}] НЕТ ТЕКСТА на страницах {_pages_of(row)} ({document})")
            problems += 1
            updated.append(row)
            continue

        if args.suggest and not row.get("anchors"):
            suggested = _candidates(row, chunks)
            if suggested:
                row["anchors"] = suggested
                print(f"[{row['id']}] якоря: {suggested}")
            else:
                print(f"[{row['id']}] кандидатов не нашлось — проставить вручную")
                problems += 1
        else:
            for anchor in row.get("anchors", []):
                if not _present_in(anchor, chunks):
                    print(f"[{row['id']}] якорь НЕ найден в чанках стр. {_pages_of(row)}: {anchor!r}")
                    problems += 1

        for problem in _fact_problems(row, chunks_by_page):
            print(f"[{row['id']}] {problem}")
            problems += 1

        updated.append(row)

    if args.suggest:
        output = PROJECT_ROOT / "reports" / "golden_dataset.with_anchors.jsonl"
        output.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in updated) + "\n",
            encoding="utf-8",
        )
        print(f"\nЗаписано: {output}")
        print("Проверить глазами и заменить исходный файл, если всё верно.")

    total = sum(1 for row in rows if str(row.get("expected") or "").lower() != "none")
    with_anchors = sum(1 for row in updated if row.get("anchors"))
    print(f"\nПозитивных записей: {total}, с якорями: {with_anchors}, проблем: {problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
