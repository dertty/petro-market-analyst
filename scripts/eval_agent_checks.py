"""Детерминированные проверки ответа агента — чистые функции, без сети.

Реализует таблицу метрик из `docs/EVAL-AGENT.md`, §3.1. Вход — то, что уже собрал
раннер: сырой markdown ответа, список вызовов инструментов и запись датасета.
Выход — список вердиктов вида `{"name", "status", "detail"}`.

Уровни: `fail` — метрика провалена и запись не засчитывается; `warn` — отклонение,
которое в pass не входит (структура ответа); `skip` — метрика к этой записи
неприменима или проверить нечем.

Отдельный модуль, а не часть `eval_run.py`, потому что проверки юнит-тестируемы без
рантайма: им нужны только строка ответа и события. Каждая метрика — своя функция,
возвращающая ноль или несколько вердиктов; `run_checks` только собирает их по порядку.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Callable, Dict, List, Optional

# Имена web-инструментов — класс, а не один инструмент: агент ходит в веб и через
# маркетплейс-скиллы. Список подтверждён прогонами 2026-08-10 (EVAL-AGENT.md, §1.6).
WEB_TOOL_MARKERS = ("perplexity", "gdelt", "keenable", "duckduckgo")

# Ссылку на отчёт собирает код поиска (skills/petro_rag/lib/search.py, Hit.citation):
# [<report>, <date>, с. <N>]. Разбираем в два шага: сначала содержимое скобок одной
# нежадной выборкой, потом расщепление справа — в названии отчёта запятая допустима,
# а хвост «дата + страницы» фиксирован. Одна регулярка на всё backtracking'ом дорога.
BRACKET = re.compile(r"\[([^\]]*)\]")
REPORT_PREFIX = "SberCIB:"
PAGES_MARKER = ", с."
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WEB_SUFFIX = re.compile(r",\s{0,3}web$")

# Обязательная часть контракта — только список источников: остальную форму ответа агент
# выбирает под вопрос, фиксированного шаблона секций больше нет.
CONTRACT_SECTIONS = ("## Источники",)

# Эвристика «ответ короткий» для out_of_scope (EVAL-AGENT.md, §1.5).
SHORT_ANSWER_CHARS = 600

METHOD_MENTION = re.compile(r"chronos|ETS|Хольт|случайн\w+ блужд|rw[ _-]?drift", re.IGNORECASE)
INTERVAL_MENTION = re.compile(r"80\s{0,3}%|q10")
PRICE_RANGE = re.compile(r"\d\s{0,3}[–—-]\s{0,3}\d")
NUMBER_TOKEN = re.compile(r"\d+(?:[.,]\d+)?")

# Окно вокруг совпадения context_regex, в котором ищется число. Решает проблему
# «15 — это дисконт, а не номер страницы» (EVAL-AGENT.md, §1.2).
FACT_WINDOW_CHARS = 150

Check = Dict[str, str]


def load_manifest(path) -> Dict[str, tuple]:
    """`reports/manifest.yaml` → {имя pdf: (report, date)}.

    Импорт PyYAML локальный и необязательный: без него проверка цитат уходит в `skip`,
    а не валит прогон — раннер должен оставаться работоспособным на голой стандартной
    библиотеке.
    """
    try:
        import yaml
    except ImportError:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except (OSError, ValueError):
        return {}
    return {
        str(document): (str(fields.get("report") or ""), str(fields.get("date") or ""))
        for document, fields in raw.items()
        if isinstance(fields, dict)
    }


def is_web_tool(name: str) -> bool:
    lowered = str(name).lower()
    return lowered.endswith("web_search") or any(marker in lowered for marker in WEB_TOOL_MARKERS)


def tool_matches(logical: str, actual: str) -> bool:
    """Имя из датасета против имени в событии.

    Скиллы приезжают с префиксом контейнера (`ext_11_r_petro_rag_rag_search`), поэтому
    сравнение по суффиксу. `web_search` — не имя, а класс инструментов.
    """
    if logical == "web_search":
        return is_web_tool(actual)
    return actual == logical or actual.endswith("_" + logical)


def report_citations(text: str) -> List[tuple]:
    """Ссылки на отчёты из текста: (report, date, множество страниц).

    Скобки, не похожие на ссылку на отчёт (нет даты или хвоста «с. N»), пропускаются:
    это веб-маркеры и прочий текст в квадратных скобках, к отчётам отношения не имеющий.
    """
    found = []
    for inner in BRACKET.findall(text):
        if not inner.startswith(REPORT_PREFIX):
            continue
        # Сначала отрезаем хвост по маркеру страниц: и название («Рынок нефти — влияние
        # конфликта на Ближнем Востоке и блокады...»), и сам список страниц («с. 49, 53»)
        # могут содержать запятые, поэтому расщеплять по ним вслепую нельзя.
        head, marker, pages = inner.rpartition(PAGES_MARKER)
        if not marker:
            continue
        report, separator, date = head.rpartition(", ")
        if not separator or not ISO_DATE.match(date.strip()):
            continue
        numbers = {int(page) for page in re.findall(r"\d+", pages)}
        if not numbers:
            continue
        found.append((report.strip(), date.strip(), numbers))
    return found


def web_citations(text: str) -> List[str]:
    """Веб-маркеры вида `[Reuters, web]`."""
    return [inner for inner in BRACKET.findall(text) if WEB_SUFFIX.search(inner)]


def _result(name: str, ok: Optional[bool], detail: str = "", level: str = "fail") -> Check:
    if ok is None:
        status = "skip"
    elif ok:
        status = "pass"
    else:
        status = level
    return {"name": name, "status": status, "detail": detail}


def _call_epoch(call: dict) -> Optional[float]:
    """Секунды из отметки вызова. `ts_epoch` кладёт раннер; `ts` — запасной разбор
    для прогонов, снятых до того, как он это начал делать."""
    value = call.get("ts_epoch")
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.datetime.fromisoformat(str(call.get("ts"))).timestamp()
    except (TypeError, ValueError):
        return None


# ─── отдельные метрики ────────────────────────────────────────────────────────


def _check_tool_choice(record: dict, text: str, calls: List[dict], manifest: dict) -> List[Check]:
    names = [str(call.get("tool") or "") for call in calls]
    missing = [
        name for name in (record.get("tools_required") or [])
        if not any(tool_matches(name, actual) for actual in names)
    ]
    forbidden = [
        name for name in (record.get("tools_forbidden") or [])
        if any(tool_matches(name, actual) for actual in names)
    ]
    detail = "; ".join(
        part for part in (
            "не вызван: " + ", ".join(missing) if missing else "",
            "вызван запрещённый: " + ", ".join(forbidden) if forbidden else "",
        ) if part
    )
    return [_result("tool_choice", not missing and not forbidden, detail)]


def _check_rag_before_web(record: dict, text: str, calls: List[dict], manifest: dict) -> List[Check]:
    if not record.get("rag_before_web"):
        return []
    rag = [
        _call_epoch(call) for call in calls
        if tool_matches("rag_search", str(call.get("tool") or ""))
    ]
    web = [_call_epoch(call) for call in calls if is_web_tool(str(call.get("tool") or ""))]
    rag = [value for value in rag if value is not None]
    web = [value for value in web if value is not None]
    if not rag or not web:
        return [_result("rag_before_web", None, "нет отметок времени у обоих классов вызовов")]
    return [_result("rag_before_web", min(rag) < min(web))]


def _check_web_budget(record: dict, text: str, calls: List[dict], manifest: dict) -> List[Check]:
    limit = record.get("max_web_searches")
    if limit is None:
        return []
    used = sum(1 for call in calls if is_web_tool(str(call.get("tool") or "")))
    return [_result("web_budget", used <= limit, f"обращений к вебу {used} при лимите {limit}")]


def _check_citation_validity(record: dict, text: str, calls: List[dict], manifest: dict) -> List[Check]:
    """Каждая ссылка на отчёт обязана сойтись с манифестом по паре report+date.

    Не сошлась — цитата галлюцинированная; цель по прогону 0. Сюда же попадает
    сокращённое название отчёта: манифест — единственный источник правильной формы.
    """
    if not manifest:
        return [_result("citation_validity", None, "манифест не прочитан (нет PyYAML?)")]
    citations = report_citations(text)
    valid = set(manifest.values())
    outside = sorted({f"{report}, {date}" for report, date, _ in citations if (report, date) not in valid})
    detail = f"вне манифеста {len(outside)} из {len(citations)}: " + "; ".join(outside) if outside else ""
    return [_result("citation_validity", not outside, detail)]


def _citation_problem(item: dict, citations: List[tuple], web_markers: List[str], manifest: dict) -> str:
    """Одно невыполненное ожидание из `expected_citations` — или пустая строка."""
    if item.get("kind") == "web":
        need = int(item.get("min_count") or 1)
        return f"web-маркеров {len(web_markers)}, нужно {need}" if len(web_markers) < need else ""
    if item.get("document"):
        report, date = manifest.get(item["document"], ("", ""))
        pages = set(item.get("pages") or [])
        hit = any(
            (found_report, found_date) == (report, date) and (not pages or found_pages & pages)
            for found_report, found_date, found_pages in citations
        )
        return "" if hit else f"нет ссылки на {item['document']}, с. {sorted(pages)}"
    need = int(item.get("min_count") or 1)
    good = sum(1 for report, date, _ in citations if (report, date) in set(manifest.values()))
    return f"валидных ссылок на отчёты {good}, нужно {need}" if good < need else ""


def _check_citation_format(record: dict, text: str, calls: List[dict], manifest: dict) -> List[Check]:
    expected = record.get("expected_citations") or []
    if not expected:
        return []
    citations = report_citations(text)
    web_markers = web_citations(text)
    problems = [
        problem for problem in
        (_citation_problem(item, citations, web_markers, manifest) for item in expected)
        if problem
    ]
    return [_result("citation_format", not problems, "; ".join(problems))]


def _fact_found(text: str, fact: dict) -> bool:
    kind = fact.get("kind")
    if kind == "regex":
        return re.search(fact["pattern"], text) is not None
    if kind == "substring":
        return fact["text"] in text
    if kind != "number":
        return False
    value = float(fact["value"])
    tolerance = float(fact.get("tolerance") or 0)
    for match in re.finditer(fact["context_regex"], text):
        window = text[max(0, match.start() - FACT_WINDOW_CHARS): match.end() + FACT_WINDOW_CHARS]
        for token in NUMBER_TOKEN.findall(window):
            if value - tolerance <= float(token.replace(",", ".")) <= value + tolerance:
                return True
    return False


def _check_facts(record: dict, text: str, calls: List[dict], manifest: dict) -> List[Check]:
    facts = record.get("expected_facts") or []
    if not facts:
        return []
    absent = [
        str(fact.get("desc") or fact.get("kind") or "факт")
        for fact in facts if not _fact_found(text, fact)
    ]
    return [_result("fact_accuracy", not absent, "не найдены: " + ", ".join(absent) if absent else "")]


def _forecast_arg_problems(expect: dict, call: Optional[dict]) -> List[str]:
    if call is None:
        return [f"нет вызова {expect['tool']}"]
    actual = call.get("args") or {}
    return [
        f"{key}={actual.get(key)!r}, ожидался {want!r}"
        for key, want in (expect.get("args") or {}).items()
        if str(actual.get(key)) != str(want)
    ]


def _check_forecast(record: dict, text: str, calls: List[dict], manifest: dict) -> List[Check]:
    expect = record.get("forecast_expect")
    if not expect:
        return []
    call = next((item for item in calls if tool_matches(expect["tool"], str(item.get("tool") or ""))), None)
    arg_problems = _forecast_arg_problems(expect, call)
    # implicit — неявный триггер инструмента: аргументы агент угадывает, и по методике
    # (§1.1) их несовпадение — предупреждение, а не провал. Отсутствие вызова — провал
    # в любом случае.
    soft = arg_problems if (record.get("implicit") and call is not None) else []
    problems = [] if soft else list(arg_problems)
    if expect.get("must_mention_method") and not METHOD_MENTION.search(text):
        problems.append("метод не назван")
    if expect.get("must_mention_interval") and not INTERVAL_MENTION.search(text):
        problems.append("интервал не приведён")
    if expect.get("must_mention_range") and not PRICE_RANGE.search(text):
        problems.append("диапазон цен не приведён")
    if expect.get("must_mention_elasticity_source") and "эластичн" not in text.lower():
        problems.append("источник эластичностей не упомянут")
    checks = [_result("forecast_protocol", not problems, "; ".join(problems))]
    if soft:
        checks.append(_result("forecast_args", False, "; ".join(soft), level="warn"))
    return checks


def _check_refusal(record: dict, text: str, calls: List[dict], manifest: dict) -> List[Check]:
    refusal = record.get("refusal_expect")
    if not refusal:
        return []
    problems = []
    limit = int(refusal.get("max_tool_calls") or 0)
    if len(calls) > limit:
        problems.append(f"вызовов {len(calls)} при лимите {limit}")
    if refusal.get("no_structure"):
        present = [section for section in CONTRACT_SECTIONS if section in text]
        if present:
            problems.append("есть секции контракта: " + ", ".join(present))
        if len(text) > SHORT_ANSWER_CHARS:
            problems.append(f"ответ длиной {len(text)} символов, ожидалось не больше {SHORT_ANSWER_CHARS}")
    return [_result("refusal_no_tools", not problems, "; ".join(problems))]


def _check_structure(record: dict, text: str, calls: List[dict], manifest: dict) -> List[Check]:
    """Предупреждение, в pass не входит. Для out_of_scope критерий обратный."""
    if record.get("scenario") == "out_of_scope":
        present = [section for section in CONTRACT_SECTIONS if section in text]
        detail = "лишние секции: " + ", ".join(present) if present else ""
        return [_result("structure", not present, detail, level="warn")]
    absent = [section for section in CONTRACT_SECTIONS if section not in text]
    return [_result("structure", not absent, "нет секций: " + ", ".join(absent) if absent else "", level="warn")]


# Порядок — как в таблице EVAL-AGENT.md, §3.1.
METRICS: tuple = (
    _check_tool_choice,
    _check_rag_before_web,
    _check_web_budget,
    _check_citation_validity,
    _check_citation_format,
    _check_facts,
    _check_forecast,
    _check_refusal,
    _check_structure,
)


def run_checks(
    record: dict,
    answer_md: str,
    tool_calls: List[dict],
    status: str,
    manifest: Optional[Dict[str, tuple]] = None,
) -> List[Check]:
    """Все применимые к записи проверки."""
    completed = status == "completed"
    checks = [_result("completed", completed, "" if completed else f"статус {status}")]
    if not completed:
        # Незавершённая задача — инфраструктурный сбой: качество по ней не меряют.
        return checks
    text = answer_md or ""
    manifest = manifest or {}
    for metric in METRICS:
        checks.extend(metric(record, text, tool_calls, manifest))
    return checks


def verdict(checks: List[Check]) -> str:
    """`pass` / `fail` / `incomplete` по списку вердиктов. Предупреждения не считаются."""
    if any(check["name"] == "completed" and check["status"] == "fail" for check in checks):
        return "incomplete"
    return "fail" if any(check["status"] == "fail" for check in checks) else "pass"
