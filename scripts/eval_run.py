#!/usr/bin/env python3
"""Прогон golden dataset через агента с сохранением ответов для ручной оценки.

    python scripts/eval_run.py                       # весь датасет
    python scripts/eval_run.py --only rag-002        # одна запись
    python scripts/eval_run.py --scenario forecast   # один сценарий
    python scripts/eval_run.py --resume eval_runs/20260810-120000  # продолжить

Транспортная часть eval_agent.py (docs/EVAL-AGENT.md, §4): задать вопрос, дождаться
ответа, собрать сырые данные. Детерминированные проверки §3.1 считает
`eval_agent_checks.py` — они попадают и в results.jsonl, и в review.md. LLM-judge пока
нет; итоговые оценки проставляются вручную в review.md, автопроверки для них подсказка.

Каждый вопрос идёт в уникальной сессии `eval-<run_id>-<record_id>` — иначе история
сессии утечёт в контекст следующего вопроса (app/main.py, /ask). Сырой markdown
ответа и usage берутся напрямую из Ouroboros (/api/tasks/{id}), а не из HTML
приложения; события tool_call дедуплицируются по tool_call_id — журнал реплеится
из двух источников и без дедупликации все вызовы задваиваются (EVAL-AGENT.md, §2).

Выход в eval_runs/<метка времени>/:
  results.jsonl — полные сырые данные, дописываются после каждой записи;
  review.md     — документ для ручной оценки, перегенерируется после каждой записи;
  run_meta.json — параметры запуска.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import os
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from eval_agent_checks import load_manifest, run_checks, verdict  # noqa: E402

APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8000")
OUROBOROS_URL = f"http://127.0.0.1:{os.environ.get('OUROBOROS_HOST_PORT', '8766')}"
NETWORK_PASSWORD = os.environ.get("OUROBOROS_NETWORK_PASSWORD", "").strip()
POLL_SEC = 5

# Сколько ждать первой активности задачи, прежде чем считать её залипшей. Рантайм
# может принять задачу и не начать её: на прогоне 2026-08-10 rag-002 простоял все
# 900 с своего дедлайна с нулём раундов и нулевой стоимостью. Дедлайн задачи тикает
# с момента создания, поэтому ждать до конца бессмысленно — дешевле отменить и
# переспросить. Нормальный старт занимает 2–3 минуты (прогрев и safety-проверки).
STALL_SEC = 360

# Быстрый детектор мертворождённой задачи. Рантайм раздаёт задачи воркерам, но после
# старта контейнера первая партия зависает: очередь считает работу розданной, воркер
# её не начинает (дефект 2026-08-10, воспроизводится при каждом пересоздании стека).
# Признак однозначный: в снимке очереди heartbeat_lag_sec равен возрасту задачи —
# воркер не подал ни одного сигнала. У живой задачи отставание в пределах 30 с.
# Лечится отменой и повтором, поэтому ждать общего порога STALL_SEC незачем.
HEARTBEAT_GRACE_SEC = 90
HEARTBEAT_DEAD_MARGIN_SEC = 10
SNAPSHOT_MAX_AGE_SEC = 150

# Оценка стоимости одной задачи для резерва бюджета при параллельном прогоне.
# Наблюдения 2026-08-10: $0.06–0.16 за вопрос; берём с запасом.
EST_TASK_USD = 0.25

# Минимальный интервал между постановками задач. POST /ask синхронно тянет список
# задач рантайма ради истории сессии, и пять одновременных запросов положили его
# в таймаут (503 на web-001 и fc-001, прогон 2026-08-10). Разносим старты.
LAUNCH_GAP_SEC = 20
_launch_lock = threading.Lock()
_last_launch = 0.0


def throttle_launch() -> None:
    """Пропустить не чаще одной постановки задачи в LAUNCH_GAP_SEC."""
    global _last_launch
    with _launch_lock:
        wait = _last_launch + LAUNCH_GAP_SEC - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_launch = time.monotonic()

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "reports" / "golden_agent_dataset.jsonl"
RUNS_DIR = ROOT / "eval_runs"

# Записи с этими статусами при --resume перезапускаются, остальные пропускаются:
# ответа они не дали и денег не стоили.
RETRY_STATUSES = {"skipped_budget", "stalled", "runner_timeout", "ask_failed"}


def _request(url: str, payload: Optional[dict] = None, bearer: bool = False) -> dict:
    """GET (payload=None) или POST json. Ошибка HTTP/сети — в поле error, не исключение."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if bearer and NETWORK_PASSWORD:
        headers["Authorization"] = f"Bearer {NETWORK_PASSWORD}"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"error": f"HTTP {exc.code}: {body[:300]}"}
    except Exception as exc:  # noqa: BLE001 — сетевые сбои не должны валить прогон
        return {"error": f"{type(exc).__name__}: {exc}"}


def runtime_context() -> Dict[str, Any]:
    """Чем и в каких условиях получен прогон: модели и остаток бюджета рантайма.

    Прогоны хранятся и сравниваются между собой, а модели задаются в .env, который
    читает docker compose, — на хосте этих переменных нет. Поэтому спрашиваем сам
    рантайм; его settings.json и есть последняя инстанция.
    """
    settings = _request(f"{OUROBOROS_URL}/api/settings", bearer=True)
    state = _request(f"{OUROBOROS_URL}/api/state", bearer=True)
    return {
        "models": {
            key.lower(): settings.get(key, "")
            for key in ("OUROBOROS_MODEL", "OUROBOROS_MODEL_LIGHT", "OUROBOROS_WEBSEARCH_MODEL")
        }
        if not settings.get("error")
        else {},
        "spent_usd_at_start": state.get("spent_usd"),
        "budget_limit": state.get("budget_limit"),
    }


def _raw_events(task_id: str) -> Optional[str]:
    """Поток событий задачи как текст. None — если поток не отдался."""
    url = f"{OUROBOROS_URL}/api/tasks/{task_id}/events?cursor=0&wait=0"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {NETWORK_PASSWORD}"} if NETWORK_PASSWORD else {}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def looks_dead(task_id: str) -> bool:
    """Мертворождённая ли задача: воркер не подал ни одного сигнала за всё её время.

    Читает снимок очереди рантайма прямо с диска — каталог проекта смонтирован в
    контейнер как /workspace, так что data/state/ виден и снаружи. Снимок обновляет
    главный цикл рантайма примерно раз в 30 с.

    Любая неопределённость (нет файла, снимок протух, задачи в нём нет) трактуется
    как «жива»: убивать задачу по догадке дороже, чем подождать общий порог.
    """
    path = ROOT / "data" / "state" / "queue_snapshot.json"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        stamped = datetime.datetime.fromisoformat(str(snapshot.get("ts")))
    except Exception:  # noqa: BLE001
        return False
    age = (datetime.datetime.now(datetime.timezone.utc) - stamped).total_seconds()
    if age > SNAPSHOT_MAX_AGE_SEC:
        return False
    for item in snapshot.get("running") or []:
        if str(item.get("id")) != task_id:
            continue
        runtime = float(item.get("runtime_sec") or 0)
        lag = float(item.get("heartbeat_lag_sec") or 0)
        return runtime >= HEARTBEAT_GRACE_SEC and lag >= runtime - HEARTBEAT_DEAD_MARGIN_SEC
    return False


def started_working(task_id: str) -> bool:
    """Начал ли агент работать: был ли хоть один раунд модели или вызов инструмента.

    Недоступный поток событий считаем активностью — молчание сети не повод
    отменять живую задачу.
    """
    raw = _raw_events(task_id)
    if raw is None:
        return True
    return '"type": "llm_round"' in raw or '"type": "tool_call"' in raw


def _epoch(value: Any) -> Optional[float]:
    """Секунды из отметки времени события.

    Рантайм отдаёт `ts` строкой ISO-8601 с часовым поясом («2026-08-10T01:39:54+00:00»),
    а не числом. Прежний `float(ts)` на ней всегда падал: `ts_span` выходил пустым,
    длительность молча бралась из настенного времени раннера вместо обещанного
    EVAL-AGENT.md §3.2, а порядок вызовов (`rag_before_web`) посчитать было нечем.
    Число всё равно принимаем — на случай, если формат когда-нибудь сменится.
    """
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _tool_events(task_id: str) -> Dict[str, Any]:
    """Вызовы инструментов из потока событий задачи + границы времени по всем событиям.

    События tool_call дедуплицируются по tool_call_id.
    """
    raw = _raw_events(task_id)
    if raw is None:
        return {"tool_calls": [], "events_error": "поток событий недоступен", "ts_span": None}

    calls: List[dict] = []
    seen: set = set()
    timestamps: List[float] = []
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        data = event.get("data") or {}
        ts = data.get("ts") or event.get("ts")
        seconds = _epoch(ts)
        if seconds is not None:
            timestamps.append(seconds)
        if event.get("type") not in {"tool_call", "tool"}:
            continue
        tool = str(data.get("tool") or data.get("name") or "")
        if not tool:
            continue
        key = data.get("tool_call_id") or (tool, str(ts), json.dumps(data.get("args"), sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        calls.append(
            {
                "tool": tool,
                "args": data.get("args"),
                # ts остаётся как пришло — его читает человек; ts_epoch считают проверки.
                "ts": ts,
                "ts_epoch": seconds,
                "is_error": bool(data.get("is_error")),
                "result_preview": data.get("result_preview"),
                "tool_call_id": data.get("tool_call_id"),
            }
        )
    span = round(max(timestamps) - min(timestamps), 1) if len(timestamps) >= 2 else None
    return {"tool_calls": calls, "events_error": None, "ts_span": span}


def task_telemetry(task: dict) -> Dict[str, Any]:
    """Поля задачи, которые идут в результат прогона.

    Токены лежат не в `usage` (такого поля у задачи нет), а плоскими
    prompt_tokens/completion_tokens — проверено на первом прогоне 2026-08-10.
    """
    tokens = {
        key: task[key]
        for key in ("prompt_tokens", "completion_tokens")
        if isinstance(task.get(key), (int, float))
    }
    return {
        "status": str(task.get("status") or ""),
        "reason_code": str(task.get("reason_code") or ""),
        "answer_md": str(task.get("result") or ""),
        "usage": tokens or None,
        "total_rounds": task.get("total_rounds"),
        "cost_usd": task.get("cost_usd"),
        "cost_final": task.get("cost_final"),
    }


def run_record(
    record: dict, run_id: str, timeout: int, attempt: int = 1, stall_sec: int = STALL_SEC
) -> dict:
    """Один вопрос со штампом времени завершения на всех путях выхода."""
    out = _ask_and_collect(record, run_id, timeout, attempt, stall_sec)
    out["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    return out


def _ask_and_collect(
    record: dict, run_id: str, timeout: int, attempt: int = 1, stall_sec: int = STALL_SEC
) -> dict:
    """Один вопрос: /ask → поллинг /result → сырые данные из Ouroboros → события."""
    record_id = record["id"]
    # Сессия уникальна и для повтора: иначе первый (залипший) заход утечёт в контекст.
    session_id = f"eval-{run_id}-{record_id}" + (f"-try{attempt}" if attempt > 1 else "")
    out: Dict[str, Any] = {
        "id": record_id,
        "scenario": record.get("scenario"),
        "question": record.get("question"),
        "session_id": session_id,
        "task_id": None,
        "status": None,
        "reason_code": "",
        "answer_md": "",
        "tool_calls": [],
        "tool_call_counts": {},
        "usage": None,
        "total_rounds": None,
        "cost_usd": None,
        "cost_final": None,
        "duration_sec": None,
        # Отметки настенного времени записи. Сумма duration_sec временем прогона не
        # является: при --parallel вопросы идут внахлёст, и сумма завышает его кратно.
        "started_at": None,
        "finished_at": None,
        "error": None,
    }

    # Ставится до throttle_launch: ожидание слота — тоже время этой записи.
    out["started_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    throttle_launch()
    created = _request(f"{APP_URL}/ask", {"question": record["question"], "session_id": session_id})
    if created.get("error") or not created.get("task_id"):
        out["status"] = "ask_failed"
        out["error"] = created.get("error") or "нет task_id в ответе /ask"
        return out
    task_id = created["task_id"]
    out["task_id"] = task_id

    started = time.monotonic()
    final = False
    working = False
    while time.monotonic() - started < timeout:
        time.sleep(POLL_SEC)
        result = _request(f"{APP_URL}/result/{task_id}")
        elapsed = int(time.monotonic() - started)
        if result.get("error"):
            print(f"    {record_id} [{elapsed:>4} с] /result: {result['error']}", flush=True)
            continue
        if result.get("final"):
            final = True
            break
        if not working:
            # Сначала быстрый признак (сердцебиение воркера), потом общий порог.
            dead = looks_dead(task_id) if elapsed >= HEARTBEAT_GRACE_SEC else False
            if not dead and elapsed >= stall_sec:
                working = started_working(task_id)
                dead = not working
            if dead:
                _request(f"{APP_URL}/cancel/{task_id}", {})
                out["status"] = "stalled"
                out["error"] = f"за {elapsed} с воркер не начал задачу, она отменена"
                out["duration_sec"] = round(time.monotonic() - started, 1)
                print(f"    {record_id} [{elapsed:>4} с] воркер не взялся за задачу — отменяю", flush=True)
                return out
        # Реже, чем поллинг: при параллели построчный шум перекрывает полезное.
        if elapsed % 60 < POLL_SEC:
            print(f"    {record_id} [{elapsed:>4} с] {result.get('status') or 'ожидание'}", flush=True)

    wall_clock = round(time.monotonic() - started, 1)
    if not final:
        _request(f"{APP_URL}/cancel/{task_id}", {})
        out["status"] = "runner_timeout"
        out["error"] = f"нет финала за {timeout} с, задача отменена"
        out["duration_sec"] = wall_clock
        return out

    # Сырой ответ и телеметрия — напрямую из Ouroboros, не answer_html приложения.
    task = _request(f"{OUROBOROS_URL}/api/tasks/{task_id}", bearer=True)
    if task.get("error"):
        out["status"] = "fetch_failed"
        out["error"] = task["error"]
        out["duration_sec"] = wall_clock
        return out
    out.update(task_telemetry(task))

    events = _tool_events(task_id)
    out["tool_calls"] = events["tool_calls"]
    if events["events_error"]:
        out["error"] = f"события не получены: {events['events_error']}"
    counts: Dict[str, int] = {}
    for call in events["tool_calls"]:
        counts[call["tool"]] = counts.get(call["tool"], 0) + 1
    out["tool_call_counts"] = counts
    out["duration_sec"] = events["ts_span"] or wall_clock
    return out


# ─── review.md ────────────────────────────────────────────────────────────────


def _quote(markdown: str) -> str:
    return "\n".join(f"> {line}" for line in (markdown or "").splitlines()) or "> (пусто)"


def _tokens_str(usage: Any) -> str:
    if not isinstance(usage, dict):
        return str(usage) if usage else "—"
    parts = []
    for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "total_tokens"):
        if isinstance(usage.get(key), (int, float)):
            parts.append(f"{key.split('_')[0]} {usage[key]:,.0f}".replace(",", " "))
    return ", ".join(parts) if parts else json.dumps(usage, ensure_ascii=False)


def _fact_hint(fact: dict) -> str:
    value = fact.get("value")
    if value is None:
        value = fact.get("pattern") or fact.get("text")
    unit = f" {fact['unit']}" if fact.get("unit") else ""
    desc = fact.get("desc") or fact.get("kind")
    return f"{desc}: `{value}`{unit}"


def _hints(record: dict) -> List[str]:
    lines: List[str] = []
    if record.get("reference_answer"):
        lines.append(f"**Эталон:** {record['reference_answer']}")
    if record.get("expected_facts"):
        lines.append("**Ожидаемые факты:** " + "; ".join(_fact_hint(f) for f in record["expected_facts"]))
    if record.get("forecast_expect"):
        lines.append("**Ожидание по прогнозу:** `" + json.dumps(record["forecast_expect"], ensure_ascii=False) + "`")
    if record.get("refusal_expect"):
        lines.append("**Ожидание по отказу:** `" + json.dumps(record["refusal_expect"], ensure_ascii=False) + "`")
    for criterion in record.get("judge_criteria") or []:
        lines.append(f"- {criterion}")
    return lines


def _models_str(models: Any) -> str:
    if not isinstance(models, dict) or not models:
        return "неизвестны (рантайм не ответил)"
    short = {"ouroboros_model": "основная", "ouroboros_model_light": "лёгкая",
             "ouroboros_websearch_model": "веб-поиск"}
    return ", ".join(f"{short.get(key, key)} `{value}`" for key, value in models.items() if value)


# Что проверяющий оценивает в каждом сценарии. Оценки бинарные: шкала «сколько из
# пяти» дрейфует между прогонами, а прогоны сравниваются между собой; кроме того
# ручная разметка служит эталоном для будущего LLM-judge, а тот по методике выдаёт
# именно бинарные вердикты. Строки по сценариям разные: спрашивать про цитаты у
# отказа на непрофильный вопрос бессмысленно.
COMMON_GRADES = [
    "Инструменты (те ли, в том ли порядке)",
    "Факты (числа верны, ничего не выдумано)",
    "Источники (цитаты валидны, веб не выдан за отчёт)",
    "Полнота (на вопрос отвечено целиком)",
]
SCENARIO_GRADES = {
    "rag": COMMON_GRADES,
    "hybrid": COMMON_GRADES,
    # Числа в вебе дрейфуют — сверяется дисциплина источников, а не значения.
    "web": [COMMON_GRADES[0], "Источники (веб помечен, дата среза указана)", COMMON_GRADES[3]],
    "forecast": [
        COMMON_GRADES[0],
        "Протокол прогноза (назван метод, приведён интервал)",
        "Подача (модельный расчёт с оговорками, не мнение)",
        COMMON_GRADES[3],
    ],
    "out_of_scope": [
        "Инструменты (лишних вызовов нет)",
        "Форма отказа (коротко, вежливо, без выдуманных фактов)",
    ],
}


def _grade_block(scenario: str) -> List[str]:
    lines = [f"**{label}:** " for label in SCENARIO_GRADES.get(scenario, COMMON_GRADES)]
    lines.append("**Итог (годен как ответ аналитика):** ")
    return "\n\n".join(lines).split("\n")


MANIFEST_PATH = ROOT / "reports" / "manifest.yaml"

CHECK_MARK = {"pass": "✅", "fail": "❌", "warn": "⚠️", "skip": "—"}


def attach_checks(result: dict, record: dict, manifest: Dict[str, tuple]) -> None:
    """Проставить записи детерминированные вердикты §3.1 (правит result на месте)."""
    result["checks"] = run_checks(
        record, result.get("answer_md") or "", result.get("tool_calls") or [],
        str(result.get("status") or ""), manifest,
    )
    result["verdict"] = verdict(result["checks"])


def _wall_clock_sec(results: List[dict]) -> Optional[float]:
    """Настенное время прогона: от первого старта до последнего финиша.

    При --parallel вопросы идут внахлёст, и сумма duration_sec завышает время прогона
    во столько раз, сколько потоков. Старые прогоны отметок не содержат — тогда None.
    """
    starts = [_epoch(r.get("started_at")) for r in results]
    ends = [_epoch(r.get("finished_at")) for r in results]
    starts = [value for value in starts if value is not None]
    ends = [value for value in ends if value is not None]
    if not starts or not ends:
        return None
    return max(ends) - min(starts)


def _checks_table(results: List[dict]) -> List[str]:
    """Сводка автопроверок по всем записям прогона."""
    graded = [r for r in results if r.get("checks")]
    if not graded:
        return []
    passed = sum(1 for r in graded if r.get("verdict") == "pass")
    counted = [r for r in graded if r.get("verdict") in {"pass", "fail"}]
    lines = [
        "## Автопроверки (детерминированные, EVAL-AGENT.md §3.1)",
        "",
        f"Прошли все применимые проверки: **{passed} из {len(counted)}**. "
        "Это не итоговая оценка — судить по-прежнему вручную ниже.",
        "",
        "| id | сценарий | вердикт | провалено | предупреждения |",
        "|---|---|---|---|---|",
    ]
    for result in graded:
        failed = [c["name"] for c in result["checks"] if c["status"] == "fail"]
        warned = [c["name"] for c in result["checks"] if c["status"] == "warn"]
        lines.append(
            f"| {result['id']} | {result['scenario']} | {result.get('verdict')} | "
            f"{', '.join(failed) or '—'} | {', '.join(warned) or '—'} |"
        )
    return lines + [""]


def write_review(path: pathlib.Path, results: List[dict], dataset: Dict[str, dict], meta: dict) -> None:
    total_cost = sum(r["cost_usd"] or 0 for r in results)
    total_sec = sum(r["duration_sec"] or 0 for r in results)
    done = sum(1 for r in results if r["status"] == "completed")
    wall = _wall_clock_sec(results)
    parallel = int(meta.get("parallel") or 1)

    timing = f"сумма длительностей {total_sec / 60:.0f} мин"
    if wall is not None:
        timing += f", настенное время прогона {wall / 60:.0f} мин"
    if parallel > 1:
        timing += f" (параллельно: {parallel})"

    lines = [
        "# Прогон golden dataset — ручная оценка",
        "",
        f"Прогон `{meta['run_id']}`, датасет `{meta['dataset']}`, старт {meta['started_at']}.",
        f"Модели: {_models_str(meta.get('models'))}.",
        f"Фильтры: {meta['filters'] or 'нет (весь датасет)'}.",
        f"Записей: {len(results)}, из них completed: {done}. "
        f"Суммарно: ${total_cost:.2f}, {timing}.",
        "",
        "Оценки бинарные: **1** — критерий выполнен, **0** — нет; итог — своё суждение, "
        "а не автоматическое «и» по строкам. Комментарий свободный.",
        "Набор строк зависит от сценария. Подсказки взяты из датасета; "
        "ответ агента приведён без изменений.",
        "",
    ]
    lines += _checks_table(results)
    for result in results:
        lines += _record_block(result, dataset.get(result["id"], {}))

    path.write_text("\n".join(lines), encoding="utf-8")


def _record_block(result: dict, record: dict) -> List[str]:
    """Раздел review.md по одной записи: телеметрия, ответ, автопроверки, поля оценок."""
    tools = ", ".join(f"{name} ×{n}" for name, n in result["tool_call_counts"].items()) or "не вызывались"
    cost = f"${result['cost_usd']:.3f}" if result["cost_usd"] is not None else "—"
    if result.get("cost_final") is False:
        cost += " (не финальная)"
    duration = f"{result['duration_sec']:.0f} с" if result["duration_sec"] else "—"

    lines = [f"## {result['id']} ({result['scenario']})", "", f"**Вопрос:** {result['question']}", ""]
    if result["status"] != "completed":
        status = result["status"] + (f" / {result['reason_code']}" if result["reason_code"] else "")
        lines += [f"⚠️ **Статус: {status}.** {result.get('error') or ''}".rstrip(), ""]
    lines += [
        f"**Инструменты:** {tools}",
        f"**Телеметрия:** {duration}, {cost}, токены: {_tokens_str(result['usage'])}"
        + (f", раундов: {result['total_rounds']}" if result.get("total_rounds") else ""),
        "",
        "**Ответ агента:**",
        "",
        _quote(result["answer_md"]),
        "",
    ]
    if result.get("checks"):
        lines += [f"**Автопроверки:** вердикт `{result.get('verdict')}`", ""]
        for check in result["checks"]:
            detail = f" — {check['detail']}" if check.get("detail") else ""
            lines.append(f"- {CHECK_MARK.get(check['status'], '?')} `{check['name']}`{detail}")
        lines.append("")
    hints = _hints(record)
    if hints:
        lines += ["**Подсказка проверяющему:**", ""] + hints + [""]
    lines += ["**Оценки (0/1):**", ""] + _grade_block(result["scenario"])
    return lines + ["", "**Комментарий:** ", "", "---", ""]


def rebuild(run_dir: pathlib.Path, dataset_path: pathlib.Path) -> int:
    """Перечитать телеметрию задач и перерисовать review.md прежнего прогона.

    Нужно, когда поменялся формат отчёта или когда учёт расхода дозакрылся уже
    после прогона (cost_final=false). Агента не дёргает — денег не стоит. Затирает
    review.md целиком, поэтому запускать до простановки оценок, а не после.
    """
    meta_path = run_dir / "run_meta.json"
    results_path = run_dir / "results.jsonl"
    if not meta_path.exists() or not results_path.exists():
        print(f"ОШИБКА: в {run_dir} нет run_meta.json или results.jsonl")
        return 1
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    with results_path.open(encoding="utf-8") as handle:
        results = [json.loads(line) for line in handle if line.strip()]

    refreshed = 0
    for result in results:
        if not result.get("task_id"):
            continue
        task = _request(f"{OUROBOROS_URL}/api/tasks/{result['task_id']}", bearer=True)
        if task.get("error"):
            print(f"  {result['id']}: задача недоступна ({task['error']})")
            continue
        result.update(task_telemetry(task))
        refreshed += 1

    dataset_by_id = {r["id"]: r for r in load_dataset(dataset_path)}
    manifest = load_manifest(MANIFEST_PATH)
    for result in results:
        attach_checks(result, dataset_by_id.get(result["id"], {}), manifest)

    results_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results), encoding="utf-8"
    )
    write_review(run_dir / "review.md", results, dataset_by_id, meta)
    print(f"Обновлено записей: {refreshed} из {len(results)}\nОценивать здесь: {run_dir / 'review.md'}")
    return 0


# ─── оркестрация ──────────────────────────────────────────────────────────────


def load_dataset(path: pathlib.Path) -> List[dict]:
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--only", help="список id через запятую")
    parser.add_argument("--scenario", choices=["rag", "web", "hybrid", "forecast", "out_of_scope"])
    parser.add_argument("--limit", type=int, help="не больше N записей после фильтров")
    parser.add_argument("--timeout", type=int, default=1000, help="секунд на вопрос (TASK_TIMEOUT_SEC=900 + запас)")
    parser.add_argument("--budget-usd", type=float, default=5.0, help="стоп по накопленной стоимости прогона")
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="сколько вопросов гнать одновременно (не больше OUROBOROS_MODEL_MAX_CONCURRENCY=3: "
        "лишние задачи будут ждать слот модели, сжигая собственный дедлайн)",
    )
    parser.add_argument(
        "--stall-sec",
        type=int,
        default=STALL_SEC,
        help="через сколько секунд без единого раунда модели считать задачу залипшей; "
        "при параллели поднимать — ожидание слота модели выглядит так же, как залипание",
    )
    parser.add_argument("--resume", help="каталог прежнего прогона — продолжить его")
    parser.add_argument(
        "--rebuild",
        help="каталог прогона: обновить телеметрию и перерисовать review.md (агента не дёргает; "
        "затирает review.md, поэтому до простановки оценок)",
    )
    args = parser.parse_args()

    if args.rebuild:
        return rebuild(pathlib.Path(args.rebuild), pathlib.Path(args.dataset))

    records = load_dataset(pathlib.Path(args.dataset))
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        records = [r for r in records if r["id"] in wanted]
        missing = wanted - {r["id"] for r in records}
        if missing:
            print(f"В датасете нет id: {', '.join(sorted(missing))}")
    if args.scenario:
        records = [r for r in records if r.get("scenario") == args.scenario]
    if args.limit is not None:
        records = records[: args.limit]

    # Каталог прогона: новый либо продолжение прежнего.
    if args.resume:
        out_dir = pathlib.Path(args.resume)
        if not (out_dir / "run_meta.json").exists():
            print(f"ОШИБКА: {out_dir} не похож на каталог прогона (нет run_meta.json)")
            return 1
        meta = json.loads((out_dir / "run_meta.json").read_text(encoding="utf-8"))
        run_id = meta["run_id"]
    else:
        run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        # Метка с точностью до секунды: два запуска подряд иначе дописывали бы
        # результаты в один каталог и смешивали прогоны.
        suffix = 0
        while (RUNS_DIR / (run_id if not suffix else f"{run_id}-{suffix}")).exists():
            suffix += 1
        run_id = run_id if not suffix else f"{run_id}-{suffix}"
        out_dir = RUNS_DIR / run_id
        out_dir.mkdir(parents=True)
        filters = ", ".join(
            part
            for part in (
                f"--only {args.only}" if args.only else "",
                f"--scenario {args.scenario}" if args.scenario else "",
                f"--limit {args.limit}" if args.limit is not None else "",
            )
            if part
        )
        meta = {
            "run_id": run_id,
            "dataset": args.dataset,
            "filters": filters,
            "timeout": args.timeout,
            "budget_usd": args.budget_usd,
            # Прогоны сравниваются между собой, а параллель растягивает длительность
            # каждой записи (ожидание слота модели) — без пометки числа несопоставимы.
            "parallel": args.parallel,
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            **runtime_context(),
        }
        (out_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    results_path = out_dir / "results.jsonl"
    results: List[dict] = []
    if results_path.exists():
        with results_path.open(encoding="utf-8") as handle:
            results = [json.loads(line) for line in handle if line.strip()]
    done_ids = {r["id"] for r in results if r["status"] not in RETRY_STATUSES}
    if len(done_ids) != len(results):
        # Записи, которые будут переиграны, выбрасываем и из файла: он дописывается
        # построчно, иначе после resume в нём остались бы прежние строки тех же id.
        results = [r for r in results if r["id"] in done_ids]
        results_path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results), encoding="utf-8"
        )
    todo = [r for r in records if r["id"] not in done_ids]

    dataset_by_id = {r["id"]: r for r in load_dataset(pathlib.Path(args.dataset))}
    manifest = load_manifest(MANIFEST_PATH)
    spent = sum(r["cost_usd"] or 0 for r in results)
    print(
        f"Прогон {run_id}: {len(todo)} вопрос(ов) "
        f"(готово ранее: {len(results)}), бюджет ${args.budget_usd:.2f}, "
        f"потрачено ${spent:.2f}\nКаталог: {out_dir}\n"
    )

    # Глобальный гвард рантайма (TOTAL_BUDGET) считает расход за всё время его жизни.
    # Упёршись в него, задачи не падают, а паркуются (budget_scope_paused) — прогон
    # молча встанет на дедлайне раннера. Дешевле предупредить на старте.
    if todo:
        state = _request(f"{OUROBOROS_URL}/api/state", bearer=True)
        left = (state.get("budget_limit") or 0) - (state.get("spent_usd") or 0)
        if not state.get("error") and left < args.budget_usd:
            print(
                f"ВНИМАНИЕ: у рантайма осталось ${left:.2f} из TOTAL_BUDGET "
                f"${state.get('budget_limit'):.2f} — меньше лимита прогона "
                f"${args.budget_usd:.2f}. Задачи начнут парковаться, когда остаток "
                "кончится; поднимите TOTAL_BUDGET в .env либо гоните частями.\n"
            )

    lock = threading.Lock()
    done_count = 0

    def flush(record_result: dict) -> None:
        nonlocal spent, done_count
        attach_checks(record_result, dataset_by_id.get(record_result["id"], {}), manifest)
        with lock:
            spent += record_result["cost_usd"] or 0
            done_count += 1
            results.append(record_result)
            with results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record_result, ensure_ascii=False) + "\n")
            write_review(out_dir / "review.md", results, dataset_by_id, meta)
            cost = f"${record_result['cost_usd']:.3f}" if record_result["cost_usd"] is not None else "$?"
            print(
                f"[{done_count}/{len(todo)}] {record_result['id']} → "
                f"{record_result['status']}  {cost}  (итого ${spent:.2f})",
                flush=True,
            )

    def budget_left(reserve: int) -> bool:
        """Влезает ли ещё одна задача с учётом уже запущенных.

        При параллели нельзя смотреть только на потраченное: задачи в полёте ещё не
        оплачены, и без резерва отсечка пропустит лишние. Резерв — оценка стоимости
        одной задачи (EST_TASK_USD) на каждую занятую нить.
        """
        with lock:
            return spent + reserve * EST_TASK_USD < args.budget_usd

    def process(record: dict, reserve_slots: int) -> None:
        if not budget_left(reserve_slots):
            flush(
                {
                    "id": record["id"], "scenario": record.get("scenario"),
                    "question": record.get("question"), "session_id": None, "task_id": None,
                    "status": "skipped_budget", "reason_code": "", "answer_md": "",
                    "tool_calls": [], "tool_call_counts": {}, "usage": None, "total_rounds": None,
                    "cost_usd": None, "cost_final": None, "duration_sec": None,
                    "error": f"пропущен: потрачено ${spent:.2f} из ${args.budget_usd:.2f}",
                }
            )
            return
        record_result = run_record(record, run_id, args.timeout, stall_sec=args.stall_sec)
        # Оба статуса означают «ответа нет и денег не потрачено»: залипшую задачу
        # рантайм не начал, а ask_failed — это 503 приложения на постановке.
        if record_result["status"] in {"stalled", "ask_failed"}:
            print(f"    {record['id']}: повтор после {record_result['status']}", flush=True)
            record_result = run_record(record, run_id, args.timeout, 2, args.stall_sec)
        flush(record_result)

    if args.parallel > 1:
        print(f"Параллельно: {args.parallel} задач(и) одновременно\n", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
            list(pool.map(lambda rec: process(rec, args.parallel), todo))
    else:
        for record in todo:
            print(f"→ {record['id']}: {record['question']}", flush=True)
            process(record, 1)

    if not results:
        # Пустая выборка (например, --limit 0): review.md всё равно должен существовать.
        write_review(out_dir / "review.md", results, dataset_by_id, meta)

    by_status: Dict[str, int] = {}
    for result in results:
        by_status[result["status"]] = by_status.get(result["status"], 0) + 1
    print("Итого:", ", ".join(f"{status}: {n}" for status, n in sorted(by_status.items())) or "пусто")
    print(f"Стоимость прогона: ${spent:.2f}")
    print(f"Оценивать здесь: {out_dir / 'review.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
