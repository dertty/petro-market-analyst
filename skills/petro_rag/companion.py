"""Резидентный процесс поиска: держит модель в памяти и обслуживает запросы по HTTP.

Почему отдельный процесс, а не модульная глобаль внутри plugin.py, как у скилла
oil_price_forecast: расширение загружается в сервер И в каждый воркер, а веса
multilingual-e5-large занимают около 2 ГБ. При десяти воркерах, каждый из которых
однажды выполнил поиск, это десятки гигабайт. Chronos-Bolt на 50 МБ такую цену не
имеет, поэтому там in-process оправдан, а здесь нет. Второе следствие: падение
onnxruntime здесь не уронит server.py.

Компаньон запускается сервером в единственном экземпляре и переживает задачи, поэтому
модель грузится один раз за жизнь контейнера.

Настройки приходят файлом, а не окружением: хост собирает окружение компаньона с нуля
по короткому белому списку, и переменные из docker compose до него не доходят. Файл
пишет plugin.py, который работает внутри сервера и окружение видит.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR))

from lib import store  # noqa: E402
from lib.embedding import make_embedder  # noqa: E402
from lib.search import DEFAULT_TOP_K, make_reranker, search  # noqa: E402

HOST = "127.0.0.1"
PORT = 8791
CONFIG_NAME = "companion-config.json"

log = logging.getLogger("petro-rag")

# Модель грузится в фоне: сервер должен принимать запросы сразу, иначе первые тридцать
# секунд после старта выглядели бы как «порт не отвечает».
_state = {"ready": False, "error": "", "chunks": 0, "embedder": "", "reranker": ""}
_lock = threading.Lock()
_engine: dict = {}


def _config() -> dict:
    state_dir = os.environ.get("OUROBOROS_SKILL_STATE_DIR", "")
    path = Path(state_dir) / CONFIG_NAME if state_dir else SKILL_DIR / CONFIG_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("не читается %s: %s", path, exc)
        return {}


def _warm_up() -> None:
    """Загрузка модели и открытие индекса. Ошибка сохраняется, а не роняет процесс."""
    config = _config()
    for key in ("PETRO_RAG_MODELS", "PETRO_RAG_INDEX", "PETRO_RAG_EMBEDDER",
                "PETRO_RAG_RERANKER", "OPENAI_API_KEY"):
        value = config.get(key)
        if value:
            os.environ.setdefault(key, str(value))

    try:
        embedder = make_embedder(os.environ.get("PETRO_RAG_EMBEDDER") or None)
        collection = store.open_collection(embedder)
        reranker = make_reranker()
        with _lock:
            _engine.update(embedder=embedder, collection=collection, reranker=reranker)
            _state.update(
                ready=True,
                chunks=collection.count(),
                embedder=embedder.id,
                reranker=os.environ.get("PETRO_RAG_RERANKER", "") or "выключен",
                error="",
            )
        log.info("готов: %s чанков, эмбеддер %s", _state["chunks"], embedder.id)
    except Exception as exc:
        with _lock:
            _state.update(ready=False, error=f"{type(exc).__name__}: {exc}")
        log.error("прогрев не удался: %s", exc)


def _reopen() -> dict:
    """Переоткрыть коллекцию тем же эмбеддером и вернуть обновлённое состояние.

    Модель при этом не перезагружается: она уже в памяти, меняется только дескриптор
    коллекции Chroma.
    """
    with _lock:
        embedder = _engine.get("embedder")
    if embedder is None:
        raise RuntimeError("эмбеддер не загружен")

    collection = store.open_collection(embedder)
    with _lock:
        _engine["collection"] = collection
        _state["chunks"] = collection.count()
        return dict(_engine)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug(fmt, *args)

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            with _lock:
                self._reply(200, dict(_state))
        else:
            self._reply(404, {"error": "unknown path"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/search":
            self._reply(404, {"error": "unknown path"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:
            self._reply(400, {"error": f"тело запроса не разбирается: {exc}"})
            return

        with _lock:
            ready = _state["ready"]
            error = _state["error"]
            engine = dict(_engine)

        if not ready:
            # 503 — временное состояние: инструмент превратит его в понятную фразу.
            self._reply(503, {"error": error or "индекс ещё прогревается"})
            return

        def run(engine_state: dict):
            return search(
                str(request.get("query") or ""),
                engine_state["collection"],
                engine_state["embedder"],
                top_k=int(request.get("top_k") or DEFAULT_TOP_K),
                # Порог не навязываем: search сам возьмёт подходящий шкале режима.
                min_score=request.get("min_score"),
                reranker=engine_state.get("reranker"),
            )

        try:
            hits = run(engine)
        except Exception:
            # Пересборка индекса удаляет коллекцию и создаёт заново, а у нас остаётся
            # ссылка на удалённую — дальше любой поиск падал бы до перезапуска
            # контейнера. Один раз переоткрываем и пробуем снова.
            log.warning("поиск не удался, переоткрываю коллекцию")
            try:
                hits = run(_reopen())
            except Exception as exc:  # noqa: BLE001
                self._reply(500, {"error": f"{type(exc).__name__}: {exc}"})
                return

        self._reply(200, {"hits": [hit.as_dict() for hit in hits]})


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s petro-rag %(levelname)s %(message)s"
    )
    threading.Thread(target=_warm_up, name="warm-up", daemon=True).start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("слушает %s:%s", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    # Возврата в норме не происходит: хост перезапускает компаньона только при
    # ненулевом коде выхода, а тихо завершившийся процесс остался бы мёртвым.
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
