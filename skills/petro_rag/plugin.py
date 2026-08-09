"""Точка входа расширения: инструмент поиска по отчётам.

Здесь намеренно нет ни моделей, ни chromadb — только stdlib и HTTP к компаньону.
Расширение загружается в процесс сервера и в каждый воркер, поэтому всё тяжёлое живёт
в отдельном процессе (см. SKILL.md, раздел про companion).

Формат ответа — JSON, как у штатных скиллов рантайма: `unix_computer_use` возвращает
`json.dumps(payload, ensure_ascii=False, indent=2)` из всех своих инструментов, включая
ветки ошибок (`{"ok": false, "error": ...}`). Указания модели — «цитируй ровно так»,
«не притягивай слабое совпадение» — живут в description инструмента, а не в теле
ответа: там же их держат штатные скиллы.

Соглашения загрузчика Ouroboros, от которых зависит работоспособность:

  * handler — второй позиционный аргумент register_tool, остальное keyword-only;
  * инструмент возвращает строку: dict доехал бы до модели как Python-repr
    (tools/extension_dispatch.py:115), поэтому json.dumps вручную;
  * инструмент асинхронный: у синхронных in-process нет таймаута вообще, а корутину
    диспетчер оборачивает в asyncio.wait_for на отдельном потоке;
  * роут, наоборот, СИНХРОННЫЙ. Асинхронный обработчик сервер выполняет прямо на своём
    event loop (gateway/extensions.py:605), синхронный уходит в asyncio.to_thread.
    Возвращённый dict сервер сам заворачивает в JSON;
  * первый параметр не должен называться ctx/context/_ctx/tool_context — иначе
    загрузчик решит, что мы просим объект контекста;
  * register() обязан оставаться дешёвым — он выполняется при старте сервера и
    каждого воркера.

Роут `search` нужен только чтобы посмотреть выдачу скилла без агента. Он отдаёт тот же
payload, что инструмент сериализует модели, и ничего под себя не меняет.
"""

from __future__ import annotations

import json
import os
import pathlib
import urllib.error
import urllib.request
from typing import Any, Optional

COMPANION_URL = "http://127.0.0.1:8791"
CONFIG_NAME = "companion-config.json"

# Компаньон читает индекс с диска и (при локальном провайдере) считает эмбеддинги на CPU.
# Замер: около 2.4 с на запрос с включённым реранкером.
REQUEST_TIMEOUT_SEC = 20

_WARMING_UP = (
    "база отчётов ещё прогревается: модель поиска загружается при первом запуске"
)


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{COMPANION_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
        return json.loads(response.read().decode("utf-8"))


def _search_payload(query: str = "", top_k: int = 8) -> dict:
    """Поиск через компаньона. Отказ возвращает полем ok/error, а не исключением.

    Инструмент не должен падать: аналитик обязан суметь ответить по вебу и честно
    сказать, что отчёты не читал.
    """
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "error": "пустой запрос: не указано, что искать"}

    try:
        payload = _post("/search", {"query": query, "top_k": int(top_k)})
    except urllib.error.HTTPError as exc:
        # 503 — компаньон жив, но ещё не прогрелся: это не поломка, а «повтори позже».
        error = _WARMING_UP if exc.code == 503 else f"служба поиска ответила HTTP {exc.code}"
        return {"ok": False, "error": error}
    except urllib.error.URLError as exc:
        # Компаньон не поднялся или перезапускается.
        return {"ok": False, "error": f"служба поиска не отвечает: {exc.reason}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {"ok": True, "hits": payload.get("hits") or []}


async def rag_search(query: str = "", top_k: int = 8) -> str:
    """Поиск по загруженным отчётам."""
    return _json(_search_payload(query, top_k))


# ─── HTTP-роут: посмотреть выдачу скилла без агента ────────────────────────────
#
# Синхронный намеренно, см. шапку модуля. Параметры берём из query string, чтобы
# смотреть curl-ом без тела запроса. Отдаёт тот же payload, что инструмент
# сериализует модели, — под себя ничего не меняет.


def route_search(request: Any) -> dict:
    params = request.query_params
    return _search_payload(params.get("query") or "", params.get("top_k") or 8)


def _write_companion_config(state_dir: Optional[str]) -> None:
    """Пробрасывает настройки компаньону через файл.

    Окружение компаньону не наследуется: хост собирает его с нуля по короткому белому
    списку, и переменные из docker compose до него не доходят. Само расширение работает
    внутри сервера и окружение видит, поэтому конфиг пишет оно.
    """
    if not state_dir:
        return
    keys = (
        "PETRO_RAG_MODELS",
        "PETRO_RAG_INDEX",
        "PETRO_RAG_EMBEDDER",
        "PETRO_RAG_RERANKER",
        "OPENAI_API_KEY",
    )
    config = {key: os.environ[key] for key in keys if os.environ.get(key)}
    path = pathlib.Path(state_dir) / CONFIG_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # Не критично: компаньон возьмёт значения по умолчанию из своего кода.
        pass


def register(api) -> None:
    info = api.get_runtime_info()
    _write_companion_config(info.get("state_dir"))

    api.register_companion_process("rag_daemon")

    api.register_tool(
        "rag_search",
        rag_search,
        description=(
            "Поиск по базе загруженных отраслевых PDF-отчётов (аналитика рынка нефти и "
            "газа). ВЫЗЫВАТЬ ПЕРВЫМ, до веб-поиска: отчёт аналитического агентства "
            "надёжнее новости. "
            "Возвращает JSON: {\"ok\": true, \"hits\": [...]} — фрагменты от лучшего к "
            "худшему. В каждом: text — дословный текст фрагмента; citation — готовая "
            "ссылка на источник, приводить её в ответе ровно в том виде, как она "
            "записана, не пересобирая; report, publisher, date, page — из чего она "
            "собрана; score — близость, чем меньше, тем слабее совпадение. "
            "Слабое совпадение не притягивать: если фрагмент не отвечает на вопрос, "
            "он не ответ. "
            "Фрагмент с \"continuation\": true и score = null — продолжение предыдущего: "
            "фраза разорвана границей страницы, и он добавлен по смежности, а не найден "
            "поиском. Читать его вместе с предыдущим, цитировать по своей citation — "
            "страница у него другая. Отсутствие score не означает слабое совпадение. "
            "Пустой hits означает, что данных по вопросу в загруженных отчётах нет — "
            "искать в вебе или прямо сказать, что отчёты тему не покрывают; цифры не "
            "выдумывать. "
            "При {\"ok\": false} смотреть error: если база прогревается, повторить вызов "
            "через минуту; иначе опираться на веб-поиск и указать в ответе, что база "
            "отчётов была недоступна."
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Что искать. Формулировать содержательно, как вопрос или тезис, "
                        "а не двумя ключевыми словами: поиск семантический."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "Сколько фрагментов вернуть. По умолчанию 8.",
                },
            },
            "required": ["query"],
        },
        # С запасом на холодный старт компаньона: первая загрузка весов с диска идёт
        # десятки секунд, дальше запрос укладывается в пару секунд.
        timeout_sec=60,
    )

    api.register_route("search", route_search, methods=("GET",))
