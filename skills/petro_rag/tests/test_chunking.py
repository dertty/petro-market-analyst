"""Нарезка: метаданные, границы страниц и — главное — нумерация страниц.

Сдвиг нумерации на единицу не проявился бы ни ошибкой, ни исключением: он выглядел бы
как «ретривер стал хуже» на eval и как чуть неверная ссылка в ответе аналитика. Поэтому
конвенция проверяется отдельным тестом против фактического содержимого отчёта.
"""

from __future__ import annotations

from lib.chunking import (
    CHUNK_CHARS,
    chunk_document,
    drop_furniture,
    iter_page_texts,
    normalize,
)


def test_normalize_collapses_whitespace_but_keeps_paragraphs():
    assert normalize("а   б\t\tв") == "а б в"
    assert normalize("абзац\n\n\n\n\nвторой") == "абзац\n\nвторой"
    assert normalize("  \n край  \n ") == "край"


def test_pages_are_one_based(strategy_pdf):
    """Первая страница отчёта — 1, а не 0."""
    numbers = [number for number, _ in iter_page_texts(strategy_pdf)]
    assert numbers, "из отчёта не извлеклось ни одной страницы с текстом"
    assert min(numbers) == 1
    assert numbers == sorted(numbers)


def test_page_numbering_matches_report_content(strategy_pdf):
    """Якорь из golden-набора лежит ровно на той странице, которой размечен.

    Запись sbercib-strategy-2026-010 указывает страницу 49 и прогноз Urals на 2026 год
    в 45 $/барр. Если бы нумерация уехала на единицу, текст нашёлся бы на 48 или 50.
    """
    by_page = {number: text for number, text in iter_page_texts(strategy_pdf)}
    assert 49 in by_page, "страница 49 не извлеклась"
    page = by_page[49].lower()
    assert "юралз" in page or "urals" in page
    assert "45" in page


def test_chunks_carry_source_metadata(strategy_pdf):
    chunks = chunk_document(
        strategy_pdf,
        report="SberCIB: Стратегический спутник",
        publisher="SberCIB Investment Research",
        date="2025-12-02",
    )
    assert chunks

    first = chunks[0]
    assert first.report == "SberCIB: Стратегический спутник"
    assert first.publisher == "SberCIB Investment Research"
    assert first.date == "2025-12-02"
    assert first.doc == strategy_pdf.name
    assert first.page >= 1

    # Метаданные для хранилища должны быть плоскими: вложенность Chroma не принимает.
    metadata = first.metadata()
    assert set(metadata) == {
        "chunk_id", "doc", "report", "publisher", "date", "page", "ordinal",
    }
    assert all(isinstance(value, (str, int)) for value in metadata.values())


def test_ordinals_are_dense_and_ordered(strategy_pdf):
    """Сквозная нумерация без дыр: по ней поиск находит соседний кусок."""
    chunks = chunk_document(strategy_pdf, report="X")
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    pages = [chunk.page for chunk in chunks]
    assert pages == sorted(pages), "порядок кусков обязан совпадать с порядком страниц"


def test_export_furniture_is_dropped_without_losing_content():
    """Колонтитул печати выкидывается построчно, содержание страницы остаётся.

    Иначе такой кусок встаёт между прозой соседних страниц, и продолжение оборванной
    фразы перестаёт быть соседом.
    """
    page = (
        "Базовый прогноз Urals на 2026 год\n"
        "Обзор рынка — SberCIBabout:reader?url=https%3A%2F%2Fsbercib.ru%2Fpublication\n"
        "Стр. 3 из 1409.08.2026, 3:37"
    )
    assert drop_furniture(page) == "Базовый прогноз Urals на 2026 год"


def test_chunk_ids_are_unique(strategy_pdf):
    chunks = chunk_document(strategy_pdf, report="X")
    identifiers = [chunk.chunk_id for chunk in chunks]
    assert len(identifiers) == len(set(identifiers))


def test_chunks_do_not_cross_page_boundaries(strategy_pdf):
    """Один чанк — одна страница: соседние страницы часто не связаны темой."""
    chunks = chunk_document(strategy_pdf, report="X")
    by_page = {number: text for number, text in iter_page_texts(strategy_pdf)}
    for chunk in chunks[:40]:
        assert chunk.text[:60] in by_page[chunk.page]


def test_chunks_fit_model_input_limit(strategy_pdf):
    """Чанк не длиннее лимита: у e5 вход 512 токенов, хвост молча отбрасывается."""
    chunks = chunk_document(strategy_pdf, report="X")
    longest = max(len(chunk.text) for chunk in chunks)
    assert longest <= CHUNK_CHARS + 200, f"самый длинный чанк {longest} символов"
