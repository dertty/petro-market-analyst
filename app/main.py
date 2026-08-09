"""Веб-приложение «Нефтегазовый аналитик» поверх task API Ouroboros.

Браузер не ходит в Ouroboros напрямую: иначе контракт ответа можно подменить
из клиента.
"""

import contextlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx
import markdown as markdown_lib
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from app import ouroboros_client as oc
from app.config import OUROBOROS_URL
from app.prompt import build_description

log = logging.getLogger("analyst")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Нефтегазовый аналитик")


@app.on_event("startup")
async def _startup() -> None:
    app.state.http = httpx.AsyncClient()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await app.state.http.aclose()


def _client() -> oc.OuroborosClient:
    return oc.OuroborosClient(app.state.http)


def _error(exc: oc.OuroborosError, status: int = 503) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=status)


def _render(text: str) -> str:
    if not text:
        return ""
    return markdown_lib.markdown(text, extensions=["tables", "fenced_code", "nl2br"])


async def _session_tasks(client: oc.OuroborosClient, session_id: str) -> List[Dict[str, Any]]:
    """Задачи одной сессии, старые первыми.

    Запроса «отдай задачи такой-то сессии» в API нет, поэтому тянем список и
    фильтруем сами. Список приходит новыми вперёд — разворачиваем.
    """
    tasks = await client.list_tasks()
    mine = [task for task in tasks if oc.session_of(task) == session_id]
    mine.sort(key=lambda item: str(item.get("ts") or ""))
    return mine


async def _history(client: oc.OuroborosClient, session_id: str) -> List[Tuple[str, str]]:
    """Пары (вопрос, ответ) завершённых задач сессии — то, что уедет в описание."""
    pairs: List[Tuple[str, str]] = []
    for task in await _session_tasks(client, session_id):
        # Только успешно завершённые: у идущей задачи в result лежит «Task is
        # running.», у упавшей — текст аварии, и это уехало бы в контекст
        # следующего вопроса как «предыдущий ответ».
        if str(task.get("status") or "").lower() != "completed":
            continue
        question = oc.question_of(task)
        answer = oc.answer_text(task)
        if question and answer:
            pairs.append((question, answer))
    return pairs


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.post("/ask")
async def ask(request: Request) -> JSONResponse:
    body = await request.json()
    question = str(body.get("question") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    if not question:
        return JSONResponse({"error": "Вопрос пуст"}, status_code=400)
    if not session_id:
        return JSONResponse({"error": "Не передан идентификатор сессии"}, status_code=400)

    client = _client()
    try:
        history = await _history(client, session_id)
        description = build_description(question, history)
        task_id = await client.create_task(description, question, session_id)
    except oc.OuroborosError as exc:
        return _error(exc)
    return JSONResponse({"task_id": task_id})


@app.get("/stream/{task_id}")
async def stream(task_id: str, request: Request) -> StreamingResponse:
    """Поток событий задачи, переложенный один в один.

    Заголовок Last-Event-ID, который браузер шлёт при переподключении, уезжает
    апстриму как cursor — события догружаются с нужного места без дублей.
    """
    cursor = 0
    with contextlib.suppress(TypeError, ValueError):
        cursor = max(0, int(request.headers.get("last-event-id") or 0))

    client = _client()

    async def relay():
        try:
            async for chunk in client.stream_events(task_id, cursor):
                yield chunk
        except oc.OuroborosError as exc:
            log.warning("stream %s: %s", task_id, exc)
        except httpx.HTTPError as exc:
            log.warning("stream %s оборвался: %s", task_id, exc)

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/result/{task_id}")
async def result(task_id: str) -> JSONResponse:
    """Состояние и готовый ответ.

    Клиент опрашивает этот эндпоинт и во время работы: терминальное событие может
    не доехать, если поток оборвётся ровно на нём, и страница зависла бы навсегда.
    """
    try:
        task = await _client().get_task(task_id)
    except oc.OuroborosError as exc:
        return _error(exc)

    status = str(task.get("status") or "")
    text = oc.answer_text(task) if oc.is_final(status) else ""
    return JSONResponse(
        {
            "status": status,
            "final": oc.is_final(status),
            "answer_html": _render(text),
            "cost_usd": oc.cost_of(task),
            # false означает, что учёт расхода ещё не закрыт
            "cost_final": bool(task.get("cost_final")),
            "reason_code": str(task.get("reason_code") or ""),
        }
    )


@app.get("/history")
async def history(session_id: str = "") -> JSONResponse:
    if not session_id:
        return JSONResponse({"error": "Не передан идентификатор сессии"}, status_code=400)
    try:
        tasks = await _session_tasks(_client(), session_id)
    except oc.OuroborosError as exc:
        return _error(exc)
    return JSONResponse(
        {
            "tasks": [
                {
                    "task_id": str(task.get("id") or ""),
                    "question": oc.question_of(task),
                    "status": str(task.get("status") or ""),
                    "ts": str(task.get("ts") or ""),
                    "cost_usd": oc.cost_of(task),
                }
                for task in tasks
            ]
        }
    )


@app.post("/cancel/{task_id}")
async def cancel(task_id: str) -> JSONResponse:
    try:
        await _client().cancel_task(task_id)
    except oc.OuroborosError as exc:
        return _error(exc)
    return JSONResponse({"ok": True})


@app.get("/ready")
async def ready() -> JSONResponse:
    """Готов ли рантайм брать задачи.

    Именно /api/state: /api/health отвечает и тогда, когда супервизор не поднят —
    например, когда не задан ключ провайдера, и задачи брать некому.
    """
    try:
        state = await _client().state()
    except oc.OuroborosError as exc:
        return JSONResponse({"ready": False, "error": str(exc)}, status_code=503)

    if not state.get("supervisor_ready"):
        return JSONResponse(
            {
                "ready": False,
                "error": (
                    "Ouroboros запущен, но не готов брать задачи: супервизор не поднят. "
                    f"Чаще всего это пустой OPENROUTER_API_KEY в .env ({OUROBOROS_URL})."
                ),
            },
            status_code=503,
        )
    return JSONResponse({"ready": True, "spent_usd": state.get("spent_usd")})
