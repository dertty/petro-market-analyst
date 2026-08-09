"""Арифметика метрики полноты из scripts/eval_rag.py.

Качество поиска здесь не проверяется — только то, что метрика считает то, что обещает.
Hit конструируется напрямую: chromadb и fastembed импортируются лениво внутри функций,
поэтому ни индекса, ни моделей для этих тестов не нужно.

Каждый тест обязан уметь упасть при откате метрики к семантике «хотя бы один» — иначе
она молча выродится в копию Anchor-Recall и будет показывать 100% на любой выдаче.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from eval_rag import _fact_coverage  # noqa: E402
from lib.search import Hit  # noqa: E402

ANCHOR_A = "Перечисленные факторы усилят инфляционное давление"
ANCHOR_B = "0,5 п. п."

ROW = {
    "document": "hormuz.pdf",
    "pages": [2, 3],
    "facts": [
        {"page": 2, "anchor": ANCHOR_A, "why": "первая половина ответа"},
        {"page": 3, "anchor": ANCHOR_B, "why": "вторая половина, после границы страниц"},
    ],
}


def _hit(page: int, text: str, doc: str = "hormuz.pdf") -> Hit:
    return Hit(
        text=text, report="SberCIB", publisher="", date="2026-03-18",
        page=page, doc=doc, chunk_id=f"h-p{page}", score=1.0,
    )


def test_partial_when_second_fact_missing():
    """Найдена половина ответа — полнота половина, а не единица."""
    hits = [_hit(2, f"текст ... {ANCHOR_A} ... хвост")]
    assert _fact_coverage(hits, ROW) == (1, 2)


def test_full_when_both_facts_present():
    hits = [_hit(2, f"... {ANCHOR_A} ..."), _hit(3, f"... {ANCHOR_B} ...")]
    assert _fact_coverage(hits, ROW) == (2, 2)


def test_overlapping_chunks_do_not_count_fact_twice():
    """Перекрытие в 150 символов кладёт якорь у границы сразу в два чанка."""
    hits = [_hit(2, f"начало {ANCHOR_A}"), _hit(2, f"{ANCHOR_A} продолжение")]
    assert _fact_coverage(hits, ROW) == (1, 2)


def test_other_document_does_not_count():
    """Тот же текст в чужом отчёте — не тот факт."""
    hits = [_hit(2, f"... {ANCHOR_A} ...", doc="strategy.pdf")]
    assert _fact_coverage(hits, ROW) == (0, 2)


def test_row_without_facts_is_not_measured():
    """Без разметки вопрос не попадает в знаменатель, а не засчитывается как ноль."""
    assert _fact_coverage([_hit(2, "что угодно")], {"document": "hormuz.pdf", "pages": [2]}) is None


def test_comparison_is_soft_on_spaces_and_case():
    """В PDF рассыпаны неразрывные пробелы, требовать побайтового совпадения нельзя."""
    hits = [_hit(2, f"... {ANCHOR_A.upper().replace(' ', '  ')} ...")]
    assert _fact_coverage(hits, ROW) == (1, 2)
