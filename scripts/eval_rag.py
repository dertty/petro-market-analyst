#!/usr/bin/env python3
"""Оценка качества поиска по размеченному набору вопросов.

    docker compose exec ouroboros python /workspace/scripts/eval_rag.py
    docker compose exec ouroboros python /workspace/scripts/eval_rag.py --threshold-sweep

Проверяется ретривер, а не ответ агента: никаких вызовов модели-генератора, только
индекс. Прогон занимает секунды, поэтому годится и как регрессионный тест после правки
нарезки или смены эмбеддера.

Метрики:
  Recall@k        — доля вопросов, где среди top-k есть чанк с ожидаемой страницей.
                    Основная метрика.
  Anchor-Recall@k — то же, но попаданием считается наличие дословного якоря в тексте
                    чанка. Устойчива к смене размера чанка: границы страниц плавают,
                    а формулировка с числом — нет.
  MRR             — насколько высоко стоит первый релевантный чанк. Проседает раньше
                    Recall и потому ловит деградацию ранжирования заранее.
  Полнота фактов  — сколько размеченных в facts фрагментов доехало до выдачи. Все метрики
                    выше останавливаются на первом попадании и потому не отличают целый
                    ответ от половины: чанк не пересекает границу страниц, и факт,
                    разорванный переносом, попадает в два чанка по половине.
  FP на негативах — доля вопросов вне корпуса, на которые поиск всё-таки что-то вернул.
                    Без неё порог отсечки подобрать нельзя: на одних позитивах оптимум
                    всегда «порог 0».
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = PROJECT_ROOT / "skills" / "petro_rag"
sys.path.insert(0, str(SKILL_DIR))

from lib import store  # noqa: E402
from lib.embedding import make_embedder  # noqa: E402
from lib.search import DEFAULT_TOP_K, Hit, make_reranker, search  # noqa: E402

GOLDEN = PROJECT_ROOT / "reports" / "golden_dataset.jsonl"


def _sweep_grid(scores: Sequence[float], steps: int = 12) -> List[float]:
    """Сетка порогов строится по наблюдённым оценкам, а не задаётся константами.

    Косинус эмбеддера и логит кросс-энкодера живут в разных шкалах (0.7-0.9 против,
    например, -10..+10), поэтому фиксированная сетка для одного из них бессмысленна.
    """
    if not scores:
        return [0.0]
    low, high = min(scores), max(scores)
    if high - low < 1e-9:
        return [low]
    step = (high - low) / steps
    return [low + step * index for index in range(steps + 1)]

_SPACES = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Мягкое сравнение: PDF щедр на неразрывные пробелы и разный регистр."""
    return _SPACES.sub(" ", text.replace(" ", " ")).strip().lower()


def _load(path: Path) -> tuple[List[dict], List[dict]]:
    if not path.exists():
        raise SystemExit(f"нет файла разметки {path}")
    positives: List[dict] = []
    negatives: List[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_number}: не разбирается JSON: {exc}") from exc
        if str(row.get("expected") or "").lower() == "none":
            negatives.append(row)
        else:
            # Старый формат с одиночным page читается как список из одного элемента.
            if "pages" not in row and "page" in row:
                row["pages"] = [row["page"]]
            positives.append(row)
    return positives, negatives


def _page_hit(hits: Sequence[Hit], row: dict) -> Optional[int]:
    """Позиция (1-based) первого чанка с ожидаемой страницей нужного документа."""
    expected_pages = {int(page) for page in row.get("pages", [])}
    document = str(row.get("document") or "")
    for position, hit in enumerate(hits, start=1):
        if document and hit.doc != document:
            continue
        if hit.page in expected_pages:
            return position
    return None


def _anchor_hit(hits: Sequence[Hit], row: dict) -> Optional[int]:
    anchors = [_normalize(a) for a in row.get("anchors", []) if a]
    if not anchors:
        return None
    for position, hit in enumerate(hits, start=1):
        text = _normalize(hit.text)
        if any(anchor in text for anchor in anchors):
            return position
    return None


def _fact_coverage(hits: Sequence[Hit], row: dict) -> Optional[tuple[int, int]]:
    """Сколько фактов вопроса доехало до выдачи: (покрыто, всего) или None без разметки.

    Знаменатель — facts: фрагменты, без которых ответ неполон, размеченные человеком
    с обоснованием. Не anchors: те подбираются регуляркой по числам как опознавательные
    метки, устойчивые к перенарезке, и у большинства вопросов якорь один — знаменатель
    выродился бы. И не pages: у страницы презентации несколько чанков, «страница
    найдена» не значит «найден чанк с фактом».

    Считает покрытым тот факт, чей якорь целиком лежит хотя бы в одном чанке выдачи.
    Именно «в одном», а не в склейке текстов: хиты отсортированы по релевантности, а не
    по порядку в документе, и на шве склейки возникает текст, которого в отчёте нет.
    Множество фактов, а не счётчик совпадений: при перекрытии чанков в 150 символов
    якорь у границы попадает сразу в два чанка и иначе считался бы дважды.

    Метрика измеряет покрытие того, что разметчик счёл обязательным, а не полноту
    ответа агента: список фактов не претендует на исчерпывающий.
    """
    facts = [f for f in row.get("facts") or [] if isinstance(f, dict) and f.get("anchor")]
    if not facts:
        return None
    document = str(row.get("document") or "")
    texts = [_normalize(hit.text) for hit in hits if not document or hit.doc == document]
    covered = sum(
        1 for fact in facts
        if any(_normalize(str(fact["anchor"])) in text for text in texts)
    )
    return covered, len(facts)


def _percent(part: int, total: int) -> str:
    return f"{part}/{total} ({(part / total * 100) if total else 0:.0f}%)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--embedder", default=None)
    parser.add_argument("--index", default=None)
    parser.add_argument("--rerank", default=None, help="Например fastembed:BAAI/bge-reranker-base")
    parser.add_argument("--candidates", type=int, default=None, help="Кандидатов до реранка")
    parser.add_argument("--no-rerank", action="store_true", help="Игнорировать PETRO_RAG_RERANKER")
    parser.add_argument("--threshold-sweep", action="store_true", help="Подобрать порог отсечки")
    parser.add_argument("--verbose", action="store_true", help="Показать промахи")
    args = parser.parse_args()

    positives, negatives = _load(GOLDEN)
    if not positives:
        raise SystemExit("в наборе нет ни одного позитивного вопроса")

    embedder = make_embedder(args.embedder)
    collection = store.open_collection(embedder, path=args.index)
    reranker = None if args.no_rerank else make_reranker(args.rerank)

    print(f"Набор: {len(positives)} позитивных, {len(negatives)} негативных")
    print(f"Эмбеддер: {embedder.id}")
    print(f"Реранкер: {args.rerank or ('выключен' if args.no_rerank else 'из окружения')}")
    print(f"top-k: {args.top_k}\n")

    # Порог применяем не в search, а после: так один прогон обслуживает всю сетку.
    #
    # Приём верен только для метрик ранжирования. С тех пор как поиск дотягивает
    # продолжения, он перестал быть верным для полноты: набор дотянутого зависит от
    # того, какие куски прошли порог, поэтому «отфильтровать потом» и «искать с
    # порогом» дают разные выдачи. Рабочий порог поэтому меряется отдельным прогоном.
    started = time.monotonic()
    hits_by_row: Dict[int, List[Hit]] = {}
    for index, row in enumerate(positives + negatives):
        kwargs = {"candidates": args.candidates} if args.candidates else {}
        hits_by_row[index] = search(
            row["query"], collection, embedder,
            top_k=args.top_k, min_score=-1.0, reranker=reranker, **kwargs,
        )
    elapsed = time.monotonic() - started
    total_queries = len(positives) + len(negatives)
    print(f"Поиск: {elapsed:.1f} с на {total_queries} запросов "
          f"({elapsed / max(total_queries, 1):.2f} с на запрос)\n")

    page_ranks: List[Optional[int]] = []
    anchor_ranks: List[Optional[int]] = []
    coverages: List[Optional[tuple[int, int]]] = []
    for index, row in enumerate(positives):
        hits = hits_by_row[index]
        page_ranks.append(_page_hit(hits, row))
        anchor_ranks.append(_anchor_hit(hits, row))
        coverages.append(_fact_coverage(hits, row))

    recall = sum(1 for rank in page_ranks if rank)
    anchor_recall = sum(1 for rank in anchor_ranks if rank)
    # MRR считается по позициям среди оценённых кусков: дотянутые продолжения ранжирование
    # не проходили, и, занимая места в списке, они сдвигали бы позиции вниз — метрика
    # ранжирования падала бы там, где ранжирование не менялось. Recall и полноту это не
    # касается: они про то, доехало ли нужное, а не про то, каким оно приехало по счёту.
    mrr = sum(
        1.0 / rank
        for rank in (
            _page_hit([h for h in hits_by_row[index] if not h.continuation], row)
            for index, row in enumerate(positives)
        )
        if rank
    ) / len(positives)
    measured = [c for c in coverages if c is not None]

    print(f"Recall@{args.top_k}:        {_percent(recall, len(positives))}")
    print(f"Anchor-Recall@{args.top_k}: {_percent(anchor_recall, len(positives))}")
    # Счётом, а не процентом: фактов единицы, и процент провоцировал бы сравнение с
    # Recall и построение тренда там, где на одно наблюдение приходятся десятки пунктов.
    if measured:
        print(f"Полнота фактов:    {sum(c for c, _ in measured)}/{sum(t for _, t in measured)} "
              f"(в {len(measured)} вопросах из {len(positives)})")
    print(f"MRR:               {mrr:.3f}")

    if args.verbose:
        for row, rank, anchor, coverage in zip(positives, page_ranks, anchor_ranks, coverages):
            short = coverage is not None and coverage[0] < coverage[1]
            if rank and anchor and not short:
                continue
            # Недобор — это не промах ранжирования: страницу нашли, а часть фактов нет.
            # Лечится нарезкой, поэтому и в выводе отделено от промаха.
            kind = "промах" if not (rank and anchor) else "недобор"
            note = f" фактов={coverage[0]}/{coverage[1]}" if coverage else ""
            print(f"  {kind} [{row['id']}] стр.{row.get('pages')} "
                  f"page_rank={rank} anchor_rank={anchor}{note}: {row['query'][:70]}")

    if args.threshold_sweep:
        observed = [hit.score for hits in hits_by_row.values() for hit in hits]
        print("\nПодбор порога (шкала зависит от того, включён ли реранкер):")
        print(f"{'порог':>8} {'Recall':>12} {'ложных срабатываний':>22}")
        for threshold in _sweep_grid(observed):
            kept_recall = sum(
                1
                for index, row in enumerate(positives)
                if (rank := _page_hit([h for h in hits_by_row[index] if h.score >= threshold], row))
            )
            false_positives = sum(
                1
                for offset in range(len(negatives))
                if [h for h in hits_by_row[len(positives) + offset] if h.score >= threshold]
            )
            print(f"{threshold:>8.3f} {_percent(kept_recall, len(positives)):>12} "
                  f"{_percent(false_positives, len(negatives)):>22}")
        print("\nВыбирать наибольший порог, который ещё не роняет Recall, "
              "при нуле ложных срабатываний.")
        print("Развёртка накладывает порог поверх одной выдачи и потому не учитывает "
              "дотянутые продолжения: они зависят от того, что порог оставил. Для "
              "полноты смотреть прогон без --threshold-sweep.")
    elif negatives:
        from lib.search import EMBED_MIN_SCORE, RERANK_MIN_SCORE

        threshold = RERANK_MIN_SCORE if reranker is not None else EMBED_MIN_SCORE
        # Отдельный прогон ровно тем путём, которым ходит агент: с порогом внутри
        # search, а не наложенным поверх готовой выдачи. Дороже на один проход, но
        # только так число описывает то, что реально доезжает до модели.
        production: Dict[int, List[Hit]] = {}
        for index, row in enumerate(positives + negatives):
            kwargs = {"candidates": args.candidates} if args.candidates else {}
            production[index] = search(
                row["query"], collection, embedder,
                top_k=args.top_k, reranker=reranker, **kwargs,
            )

        false_positives = sum(
            1 for offset in range(len(negatives)) if production[len(positives) + offset]
        )
        kept = sum(
            1 for index, row in enumerate(positives) if _page_hit(production[index], row)
        )
        kept_coverage = [
            c for c in (
                _fact_coverage(production[index], row)
                for index, row in enumerate(positives)
            ) if c is not None
        ]
        coverage_note = ""
        if kept_coverage:
            coverage_note = (f", полнота фактов {sum(c for c, _ in kept_coverage)}/"
                             f"{sum(t for _, t in kept_coverage)}")
        print(f"\nНа рабочем пороге {threshold}: Recall {_percent(kept, len(positives))}, "
              f"ложных срабатываний на негативах {_percent(false_positives, len(negatives))}"
              f"{coverage_note}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
