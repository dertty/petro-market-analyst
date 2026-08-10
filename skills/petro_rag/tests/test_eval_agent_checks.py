"""Разбор ссылок на отчёты и `citation_validity` из scripts/eval_agent_checks.py.

Проверки чистые: ни сети, ни рантайма, ни индекса — только строка ответа и манифест.

Смысл набора — не дать разбору снова начать молча выбрасывать цитаты. Пока в нём стоял
фильтр по издателю (`REPORT_PREFIX = "SberCIB:"`), ссылка вида `[SberCIB, <дата>, с. N]`
не разбиралась вовсе, и блокирующая `citation_validity` проходила вхолостую: цитат не
найдено — значит и невалидных нет. Каждый тест ниже обязан уметь упасть при возврате
любого фильтра по названию издателя.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from eval_agent_checks import _check_citation_validity, report_citations  # noqa: E402

STRATEGY = ("SberCIB: Стратегический спутник — прогнозы на 2026", "2025-12-02")
MARKET = ("SberCIB: Нефтяной рынок в 2026 году — аналитический обзор и прогноз", "2026-02-25")
MANIFEST = {"SberCIB-Strategiya-2026.pdf": STRATEGY, "neftyanoi-rinok.pdf": MARKET}


def _validity(text: str) -> str:
    return _check_citation_validity({}, text, [], MANIFEST)[0]["status"]


def test_valid_citation_passes():
    """Название и дата ровно как в манифесте — цитата разобрана и засчитана."""
    text = f"Прогноз $45/барр. [{MARKET[0]}, {MARKET[1]}, с. 9]."
    assert report_citations(text) == [(MARKET[0], MARKET[1], {9})]
    assert _validity(text) == "pass"


@pytest.mark.parametrize(
    "citation",
    [
        # Наблюдалось в прогонах 023914, 033117 и 072643: агент сокращает название отчёта
        # до одного издателя. Форма ссылки верна, содержание — выдумано.
        "SberCIB, 2026-02-25, с. 9",
        # Сокращение до неполного названия — тот же класс.
        "SberCIB: Нефтяной рынок, 2026-02-25, с. 7",
        # Отчёт, которого в корпусе нет вовсе.
        "IEA Oil Market Report, 2026-04-01, с. 12",
        # PDF без записи в манифесте: название собрано из слага имени файла.
        "Neftyanoi rinok v 2026 godu, 2026-02-25, с. 7",
        # Дата, которой у этого отчёта нет.
        f"{MARKET[0]}, 2026-05-01, с. 9",
    ],
)
def test_citation_outside_manifest_fails(citation):
    """Разобрано, но с манифестом не сошлось — галлюцинация, метрика обязана упасть."""
    text = f"Согласно оценке [{citation}] спрос снизится."
    assert len(report_citations(text)) == 1
    assert _validity(text) == "fail"


@pytest.mark.parametrize(
    "bracket",
    [
        "Reuters, web",  # веб-маркер: ни даты, ни страницы
        "Reuters, 2026-08-01",  # веб-источник в секции «Источники»: даты есть, страницы нет
        "текст ссылки",  # обычная markdown-ссылка [текст](url)
    ],
)
def test_non_report_brackets_are_not_citations(bracket):
    """Снятие фильтра по издателю не должно втянуть в разбор посторонние скобки."""
    text = f"По данным [{bracket}] рынок вырос."
    assert report_citations(text) == []
    assert _validity(text) == "pass"


@pytest.mark.parametrize(
    "bracket",
    [
        "там же, с. 13",  # промпт прямо запрещает «там же»
        "SberCIB, с. 9, 13",  # без даты
        "2026-03-18, с. 2",  # без названия
    ],
)
def test_malformed_citations_are_known_blind_spot(bracket):
    """Сломанная форма ссылки не разбирается и метрикой не видна — известный пробел.

    Тест фиксирует текущее поведение, а не одобряет его: все три формы встречались в
    прогонах. Когда неразобранные кандидаты начнут отдаваться отдельным вердиктом,
    этот тест обязан упасть и потребовать обновления.
    """
    text = f"Как отмечено выше [{bracket}], добыча растёт."
    assert report_citations(text) == []
    assert _validity(text) == "pass"


def test_manifest_unread_is_skip_not_pass():
    """Без манифеста сверять не с чем — `skip`, а не молчаливый `pass`."""
    text = f"[{MARKET[0]}, {MARKET[1]}, с. 9]"
    assert _check_citation_validity({}, text, [], {})[0]["status"] == "skip"
