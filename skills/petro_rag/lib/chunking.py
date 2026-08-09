"""Нарезка PDF на чанки с метаданными источника.

Чанк никогда не пересекает границу страниц: в презентациях и подобных отчётах соседние
страницы часто не связаны темой — разные слайды держат разные сюжеты, — и чанк,
склеенный из двух таких страниц, размыл бы вектор между темами. Зависит от документа:
в отчётах со сплошным текстом это ограничение не так важно, но нарезка не различает
тип документа. Плата за это одна и та же в обоих случаях — факт, разорванный переносом
на следующую страницу, попадёт в два чанка по половине; eval-набор поэтому допускает
список страниц на вопрос. Склеивать такие половины в «мостовые» чанки не понадобилось:
разрыв лечится на стороне поиска, где продолжение дотягивается к принятому куску по
смежности (`search._expand_torn_neighbours`) — это не трогает ни оценки, ни цитаты.

Нумерация страниц — 1-based, как в reader'е и в разметке golden-набора. Это
зафиксировано тестом: сдвиг на единицу превратил бы весь eval в бессмыслицу, но
выглядел бы как «плохой ретривер».
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional

# Предел входа у multilingual-e5-large — 512 токенов; всё сверх молча отбрасывается
# при кодировании. Размер в символах подобран так, чтобы русский текст с запасом
# укладывался в лимит (у многоязычного токенизатора это примерно 250-350 токенов).
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150

# Страницы короче этого — колонтитулы, номера слайдов, пустые листы после сканов.
MIN_PAGE_CHARS = 120

_WHITESPACE = re.compile(r"[ \t ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclasses.dataclass(frozen=True)
class Chunk:
    """Фрагмент отчёта вместе со всем, что нужно для ссылки на источник."""

    text: str
    chunk_id: str
    doc: str
    report: str
    publisher: str
    date: str
    page: int
    # Сквозной номер куска внутри документа. Нужен поиску, чтобы найти соседа по тексту:
    # без него порядок пришлось бы восстанавливать разбором chunk_id, и поиск оказался бы
    # завязан на формат идентификатора.
    ordinal: int = 0

    def metadata(self) -> Dict[str, object]:
        """Плоский dict для векторного хранилища: вложенности оно не принимает."""
        return {
            "chunk_id": self.chunk_id,
            "doc": self.doc,
            "report": self.report,
            "publisher": self.publisher,
            "date": self.date,
            "page": self.page,
            "ordinal": self.ordinal,
        }


def normalize(text: str) -> str:
    """Схлопывает пробелы и лишние переводы строк, оставляя абзацы различимыми."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


# Колонтитулы браузерной печати. Часть отчётов — веб-статьи, распечатанные в PDF через
# режим чтения, и на каждой странице повторяются строка с about:reader и строка
# «Стр. N из M» с датой печати. Содержания в них нет, а вреда два: они всплывают в
# выдаче отдельными кусками и, главное, встают между прозой соседних страниц — тогда
# продолжение оборванной фразы перестаёт быть соседом, и дотянуть его нечем.
_FURNITURE = (
    re.compile(r"about:reader\?url="),
    re.compile(r"^Стр\.\s*\d+\s*из\s*\d+"),
)


def drop_furniture(text: str) -> str:
    """Убирает строки колонтитулов, оставляя содержание страницы нетронутым.

    Построчно, а не куском: у первой страницы колонтитул приклеен к оглавлению и
    вступлению, и выбрасывать весь фрагмент значило бы потерять текст отчёта.
    """
    return "\n".join(
        line for line in text.split("\n")
        if not any(pattern.search(line) for pattern in _FURNITURE)
    )


def _splitter():
    """Сплиттер LangChain: иерархия разделителей (абзац → строка → фраза → слово).

    Импорт внутри функции, чтобы модуль оставался пригодным для тестов нарезки
    метаданных там, где langchain не установлен.
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_CHARS,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
        keep_separator=True,
    )


def iter_page_texts(pdf_path: Path) -> Iterator[tuple[int, str]]:
    """Страницы документа как (номер 1-based, нормализованный текст).

    Страницы без извлекаемого текста пропускаются молча: в презентациях часть
    страниц — картинки целиком, и это не ошибка.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    for index, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception:
            # Битая страница не должна ронять индексацию всего отчёта.
            continue
        text = drop_furniture(normalize(raw))
        if len(text) >= MIN_PAGE_CHARS:
            yield index, text


def chunk_document(
    pdf_path: Path,
    *,
    report: str,
    publisher: str = "",
    date: str = "",
    doc: Optional[str] = None,
) -> List[Chunk]:
    """Все чанки одного отчёта, в порядке страниц."""
    doc_name = doc or pdf_path.name
    stem = re.sub(r"[^a-zA-Z0-9]+", "-", pdf_path.stem).strip("-").lower()[:40]
    splitter = _splitter()

    chunks: List[Chunk] = []
    for page_number, page_text in iter_page_texts(pdf_path):
        for position, piece in enumerate(splitter.split_text(page_text)):
            piece = piece.strip()
            if len(piece) < MIN_PAGE_CHARS // 2:
                continue
            chunks.append(
                Chunk(
                    text=piece,
                    chunk_id=f"{stem}-p{page_number}-{position}",
                    doc=doc_name,
                    report=report,
                    publisher=publisher,
                    date=date,
                    page=page_number,
                    ordinal=len(chunks),
                )
            )
    return chunks
