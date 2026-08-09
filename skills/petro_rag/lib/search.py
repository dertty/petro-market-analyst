"""Поиск по индексу отчётов: кандидаты → опциональный реранк → отсечка → продолжения.

Реранкер по умолчанию выключен. Корпус небольшой, и лишняя ступень стоит секунд
процессорного времени на каждый запрос; включать её имеет смысл только если eval
покажет, что голых эмбеддингов не хватает. Переключается переменной окружения
PETRO_RAG_RERANKER, устроенной так же, как выбор эмбеддера.

Порог отсечки существует ради вопросов вне корпуса: без него на «цену лития» вернутся
три уверенных куска про Urals, и аналитик их процитирует. Значение подбирается на
eval-наборе с негативными примерами, а не на глаз.
"""

from __future__ import annotations

import dataclasses
import os
from typing import List, Optional

from .embedding import Embedder

# Сколько достать из индекса до реранка. Без реранка берётся сразу top_k.
# 15, а не 30: замер показал, что качество на 15 и 30 одинаковое (Recall@5 79%), а
# кросс-энкодер линеен по числу пар — 2.4 с против 4.6 с на запрос.
CANDIDATES = 15
DEFAULT_TOP_K = 8

# Пороги отсечки живут в разных шкалах и потому раздельные: у эмбеддера это косинус
# (0..1), у кросс-энкодера — логит (примерно -2.5..+2.5). Один общий порог означал бы,
# что при включении реранкера отсекается почти всё.
#
# Значения получены прогоном scripts/eval_rag.py --threshold-sweep на наборе с
# негативными примерами; распределение разобрано в skills/petro_rag/tests/README.md.
# Идеального разделения нет: у части правильных фрагментов оценка ниже, чем у лучшего
# негатива, поэтому порог выбран в пользу полноты — пропущенный факт из отчёта
# противоречит цели «сначала отчёты», а лишний слабый фрагмент агент отбросит сам,
# прочитав текст.
EMBED_MIN_SCORE = 0.80
RERANK_MIN_SCORE = -0.5


@dataclasses.dataclass(frozen=True)
class Hit:
    text: str
    report: str
    publisher: str
    date: str
    page: int
    doc: str
    chunk_id: str
    score: float
    ordinal: int = 0
    # Кусок, добавленный как продолжение соседа, а не найденный поиском. Своей оценки
    # у него нет: его взяли по смежности, и выдавать чужую оценку за его собственную
    # значило бы врать о том, насколько он подошёл запросу.
    continuation: bool = False

    def citation(self) -> str:
        """Ссылка на источник в том виде, в каком она должна попасть в ответ."""
        parts = [self.report]
        if self.date:
            parts.append(self.date)
        parts.append(f"с. {self.page}")
        return f"[{', '.join(parts)}]"

    def as_dict(self) -> dict:
        data = {**dataclasses.asdict(self), "citation": self.citation()}
        if self.continuation:
            data["score"] = None
        return data


def make_reranker(spec: Optional[str] = None):
    """Кросс-энкодер по строке `<провайдер>:<модель>` либо None, если выключен."""
    spec = (spec if spec is not None else os.environ.get("PETRO_RAG_RERANKER", "")).strip()
    if not spec:
        return None
    provider, _, model = spec.partition(":")
    if provider != "fastembed" or not model:
        raise ValueError(
            f"неизвестный реранкер '{spec}', ожидается 'fastembed:<модель>' или пустая строка"
        )
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(
        model_name=model, cache_dir=os.environ.get("PETRO_RAG_MODELS") or None
    )


def _hits_from_chroma(result: dict) -> List[Hit]:
    """Разбирает ответ Chroma. distances по косинусу: score = 1 - distance."""
    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]

    hits: List[Hit] = []
    for text, meta, distance in zip(documents, metadatas, distances):
        meta = meta or {}
        hits.append(
            Hit(
                text=text or "",
                report=str(meta.get("report") or meta.get("doc") or "источник"),
                publisher=str(meta.get("publisher") or ""),
                date=str(meta.get("date") or ""),
                page=int(meta.get("page") or 0),
                doc=str(meta.get("doc") or ""),
                chunk_id=str(meta.get("chunk_id") or ""),
                score=1.0 - float(distance),
                ordinal=int(meta.get("ordinal") or 0),
            )
        )
    return hits


def _is_torn(earlier_text: str) -> bool:
    """Обрывается ли текст на полуслове, то есть продолжается ли он в следующем куске.

    Двоеточие считается обрывом наравне с отсутствием знака: висящий в конце страницы
    заголовок вида «Факторы влияния на цены:» — тот же разрыв, только содержание списка
    осталось на следующей странице.
    """
    tail = (earlier_text or "").rstrip()
    return bool(tail) and tail[-1] not in ".!?»"


def _expand_torn_neighbours(hits: List[Hit], collection) -> List[Hit]:
    """Дотягивает смежный кусок к тем, что уже прошли порог.

    Зачем: чанк не пересекает границу страниц, а перекрытия между страницами нет вовсе,
    поэтому разорванная переносом фраза не покрыта целиком ни одним куском. Вторую
    половину такого ответа кросс-энкодер оценивает низко — она вырвана из контекста, —
    и порог её срезает, хотя первая половина принята. Замеры: 11 размеченных фактов из
    12 попадают в выдачу, а до агента доезжают 7 (`skills/petro_rag/tests/README.md`).

    Сосед берётся ПО СМЕЖНОСТИ, а не по своей оценке, и только когда текст на стыке
    действительно разорван. Это принципиально дешевле и безопаснее, чем склеивать куски
    до реранкера: там объединённая оценка решала бы судьбу обеих половин сразу и могла
    бы отбросить ту, что сейчас проходит.
    """
    if not hits:
        return hits

    # Что могло бы понадобиться: сосед слева и справа у каждого принятого куска.
    wanted = {
        (hit.doc, hit.ordinal + shift)
        for hit in hits
        for shift in (-1, 1)
        if hit.ordinal + shift >= 0
    }
    present = {(hit.doc, hit.ordinal) for hit in hits}
    wanted -= present
    if not wanted:
        return hits

    try:
        found = collection.get(
            where={
                "$and": [
                    {"doc": {"$in": sorted({doc for doc, _ in wanted})}},
                    {"ordinal": {"$in": sorted({ordinal for _, ordinal in wanted})}},
                ]
            },
            include=["documents", "metadatas"],
        )
    except Exception:
        # Дотягивание — улучшение поверх основного результата. Если хранилище его не
        # поддержало, выдача обязана остаться прежней, а не пропасть.
        return hits

    by_key = {}
    for text, meta in zip(found.get("documents") or [], found.get("metadatas") or []):
        meta = meta or {}
        by_key[(str(meta.get("doc") or ""), int(meta.get("ordinal") or 0))] = (text or "", meta)

    out: List[Hit] = []
    added = set(present)
    for hit in hits:
        out.append(hit)
        for shift in (-1, 1):
            key = (hit.doc, hit.ordinal + shift)
            if key in added or key not in by_key:
                continue
            text, meta = by_key[key]
            # Разорван ли стык, решает текст того куска, который идёт раньше.
            earlier = hit.text if shift == 1 else text
            if not _is_torn(earlier):
                continue
            added.add(key)
            out.append(
                Hit(
                    text=text,
                    report=str(meta.get("report") or meta.get("doc") or "источник"),
                    publisher=str(meta.get("publisher") or ""),
                    date=str(meta.get("date") or ""),
                    page=int(meta.get("page") or 0),
                    doc=str(meta.get("doc") or ""),
                    chunk_id=str(meta.get("chunk_id") or ""),
                    score=hit.score,
                    ordinal=int(meta.get("ordinal") or 0),
                    continuation=True,
                )
            )
    return out


def search(
    query: str,
    collection,
    embedder: Embedder,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: Optional[float] = None,
    reranker=None,
    candidates: int = CANDIDATES,
) -> List[Hit]:
    """Релевантные фрагменты, от лучшего к худшему. Пустой список — ответа в корпусе нет.

    `candidates` — сколько взять из индекса до реранка. Кросс-энкодер считает каждую
    пару «запрос-фрагмент» отдельно, поэтому его время линейно по этому числу, и оно
    подбирается замером: см. skills/petro_rag/tests/README.md.

    `min_score` без значения выбирается по режиму: шкалы косинуса и кросс-энкодера
    несопоставимы, и общий порог означал бы, что при включении реранкера отсекается всё.

    Итог может оказаться длиннее `top_k`: к принятым кускам дотягиваются их продолжения,
    оборванные границей страницы. Такие фрагменты помечены `continuation` и своей оценки
    не имеют — см. `_expand_torn_neighbours`.
    """
    query = (query or "").strip()
    if not query:
        return []

    if min_score is None:
        min_score = RERANK_MIN_SCORE if reranker is not None else EMBED_MIN_SCORE

    wanted = max(candidates, top_k) if reranker is not None else max(top_k, 1)
    raw = collection.query(
        query_embeddings=[embedder.query(query)],
        n_results=wanted,
        include=["documents", "metadatas", "distances"],
    )
    hits = _hits_from_chroma(raw)

    if reranker is not None and hits:
        # Кросс-энкодер оценивает пару «запрос-фрагмент» целиком, поэтому его оценки
        # живут в своей шкале и заменяют косинусные, а не смешиваются с ними.
        scores = list(reranker.rerank(query, [hit.text for hit in hits]))
        hits = [
            dataclasses.replace(hit, score=float(score))
            for hit, score in zip(hits, scores)
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)

    kept = [hit for hit in hits if hit.score >= min_score][:top_k]
    return _expand_torn_neighbours(kept, collection)
