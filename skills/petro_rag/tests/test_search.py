"""Механика хранилища и поиска на фейковом эмбеддере — без моделей и сети.

Качество поиска здесь не проверяется (мешок слов для этого не годится): им занимается
scripts/eval_rag.py на настоящей модели. Здесь проверяется всё остальное — что метаданные
доезжают до цитаты, что порог отсекает, что смена эмбеддера ловится, и что пустая выдача
объясняется словами, а не молчанием.
"""

from __future__ import annotations

import pytest

from lib import store
from lib.chunking import Chunk
from lib.search import Hit, search

CHUNKS = [
    Chunk(
        text="Базовый прогноз средней цены Urals на 2026 год — 45 долларов за баррель.",
        chunk_id="rep-p13-0",
        doc="report.pdf",
        report="SberCIB: Нефтяной рынок в 2026 году",
        publisher="SberCIB Investment Research",
        date="",
        page=13,
    ),
    Chunk(
        text="Блокада Ормузского пролива на два-три месяца поднимает Brent до 81 доллара.",
        chunk_id="rep-p2-0",
        doc="hormuz.pdf",
        report="SberCIB: Рынок нефти",
        publisher="SberCIB Investment Research",
        date="2025-12-02",
        page=2,
    ),
]


@pytest.fixture()
def collection(tmp_path, fake_embedder):
    written = store.build(CHUNKS, fake_embedder, path=str(tmp_path / "index"))
    assert written == len(CHUNKS)
    return store.open_collection(fake_embedder, path=str(tmp_path / "index"))


def test_search_returns_hit_with_source_metadata(collection, fake_embedder):
    hits = search(
        "Базовый прогноз средней цены Urals на 2026 год",
        collection,
        fake_embedder,
        min_score=0.0,
    )
    assert hits
    best = hits[0]
    assert best.page == 13
    assert best.report.startswith("SberCIB")
    assert "Urals" in best.text


def test_citation_format(collection, fake_embedder):
    """Ссылка выглядит как [отчёт, дата, с. N]; без даты — просто без неё."""
    dated = Hit(
        text="", report="SberCIB: Рынок нефти", publisher="", date="2025-12-02",
        page=2, doc="", chunk_id="", score=0.9,
    )
    assert dated.citation() == "[SberCIB: Рынок нефти, 2025-12-02, с. 2]"

    undated = Hit(
        text="", report="Обзор", publisher="", date="", page=7, doc="", chunk_id="", score=0.5,
    )
    assert undated.citation() == "[Обзор, с. 7]"


def test_threshold_filters_everything_out(collection, fake_embedder):
    """Недостижимый порог обязан давать пустую выдачу, а не «хоть что-нибудь»."""
    assert search("Urals", collection, fake_embedder, min_score=1.01) == []


def test_empty_query_returns_nothing(collection, fake_embedder):
    assert search("   ", collection, fake_embedder, min_score=0.0) == []


def test_top_k_limits_results(collection, fake_embedder):
    hits = search("нефть баррель", collection, fake_embedder, top_k=1, min_score=0.0)
    assert len(hits) <= 1


def test_index_rejects_foreign_embedder(tmp_path, fake_embedder):
    """Индекс, собранный другой моделью, не должен молча использоваться."""
    path = str(tmp_path / "index")
    store.build(CHUNKS, fake_embedder, path=path)

    class OtherEmbedder:
        id = "openai:text-embedding-3-small"
        dim = 1536

        def passages(self, texts):
            raise AssertionError("не должно вызываться")

        def query(self, text):
            raise AssertionError("не должно вызываться")

    with pytest.raises(RuntimeError, match="пересоберите индекс"):
        store.open_collection(OtherEmbedder(), path=path)


def test_missing_index_reports_how_to_build(tmp_path, fake_embedder):
    with pytest.raises(RuntimeError, match="build_index"):
        store.open_collection(fake_embedder, path=str(tmp_path / "nothing-here"))


# ─── Дотягивание продолжений ───────────────────────────────────────────────────
#
# Стык страниц перекрытием не покрыт, поэтому разорванная фраза лежит в двух кусках.
# Проверяется, что второй приезжает по смежности и что целые фразы соседей не тянут.

SPLIT = [
    Chunk(
        text="санкции подняли дисконт среднегодовая цена экспортируемой российской нефти в 2026",
        chunk_id="split-p1-0", doc="split.pdf", report="Обзор",
        publisher="", date="", page=1, ordinal=0,
    ),
    Chunk(
        text="году составит 47 долларов за баррель, консервативная оценка аналитиков.",
        chunk_id="split-p2-0", doc="split.pdf", report="Обзор",
        publisher="", date="", page=2, ordinal=1,
    ),
    Chunk(
        text="удобрения алюминий спг разбираются отдельным блоком.",
        chunk_id="split-p3-0", doc="split.pdf", report="Обзор",
        publisher="", date="", page=3, ordinal=2,
    ),
]

SPLIT_QUERY = "санкции подняли дисконт среднегодовая цена экспортируемой российской нефти"


@pytest.fixture()
def split_collection(tmp_path, fake_embedder):
    store.build(SPLIT, fake_embedder, path=str(tmp_path / "split"))
    return store.open_collection(fake_embedder, path=str(tmp_path / "split"))


def test_torn_neighbour_is_pulled_in(split_collection, fake_embedder):
    """Кусок оборван на полуслове — продолжение обязано приехать следом."""
    hits = search(SPLIT_QUERY, split_collection, fake_embedder, top_k=1, min_score=0.0)
    assert [hit.chunk_id for hit in hits] == ["split-p1-0", "split-p2-0"]
    assert hits[1].continuation is True
    assert hits[1].page == 2, "у продолжения своя страница, цитата не должна съезжать"


def test_continuation_has_no_score_of_its_own(split_collection, fake_embedder):
    """Оценки у дотянутого куска нет: его взяли по смежности, а не по релевантности."""
    hits = search(SPLIT_QUERY, split_collection, fake_embedder, top_k=1, min_score=0.0)
    assert hits[1].as_dict()["score"] is None
    assert hits[0].as_dict()["score"] is not None


def test_whole_sentence_neighbour_is_not_pulled(split_collection, fake_embedder):
    """Сосед через законченную фразу — это другая тема, тянуть его незачем."""
    hits = search("удобрения алюминий спг отдельным блоком", split_collection,
                  fake_embedder, top_k=1, min_score=0.0)
    assert [hit.chunk_id for hit in hits] == ["split-p3-0"]


def test_expansion_does_not_reach_into_another_document(collection, fake_embedder):
    """Куски разных отчётов не соседи, даже если номера подряд."""
    hits = search("Базовый прогноз средней цены Urals", collection, fake_embedder,
                  top_k=1, min_score=0.0)
    assert all(hit.doc == hits[0].doc for hit in hits)
