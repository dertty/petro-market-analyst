#!/usr/bin/env python3
"""Задать вопрос аналитику из командной строки и дождаться ответа.

    python scripts/ask.py "Какой прогноз SberCIB по цене Urals на 2026 год?"
    python scripts/ask.py --session demo-1 "А по Brent то же самое?"

Тот же путь, что и у страницы: POST /ask в приложение, затем опрос /result. Нужен для
сквозных проверок и для записи демонстрационных сценариев — в браузере их неудобно
воспроизводить дословно.

Печатает, какие инструменты агент вызвал: без этого невозможно отличить ответ, собранный
по отчётам, от ответа, придуманного по памяти модели.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8000")
OUROBOROS_URL = f"http://127.0.0.1:{os.environ.get('OUROBOROS_HOST_PORT', '8766')}"
POLL_SEC = 5


def _request(url: str, payload: Optional[dict] = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"error": f"HTTP {exc.code}: {body[:300]}"}


def _tool_calls(task_id: str) -> list:
    """Имена вызванных инструментов из потока событий задачи."""
    url = f"{OUROBOROS_URL}/api/tasks/{task_id}/events?cursor=0&wait=0"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []

    names = []
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        data = event.get("data") or {}
        name = data.get("tool") or data.get("name") or ""
        if event.get("type") in {"tool_call", "tool"} and name:
            names.append(str(name))
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument("--session", default="cli")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    created = _request(f"{APP_URL}/ask", {"question": args.question, "session_id": args.session})
    if created.get("error") or not created.get("task_id"):
        print(f"Не удалось создать задачу: {created.get('error')}")
        return 1

    task_id = created["task_id"]
    print(f"Задача {task_id}\nВопрос: {args.question}\n")

    started = time.monotonic()
    while time.monotonic() - started < args.timeout:
        time.sleep(POLL_SEC)
        result = _request(f"{APP_URL}/result/{task_id}")
        elapsed = int(time.monotonic() - started)
        if not result.get("final"):
            print(f"  [{elapsed:>4} с] {result.get('status') or 'ожидание'}", flush=True)
            continue

        tools = _tool_calls(task_id)
        print(f"\nСтатус: {result.get('status')}  за {elapsed} с  "
              f"${result.get('cost_usd') or 0:.4f}")
        if tools:
            print(f"Инструменты: {', '.join(tools)}")
        else:
            print("Инструменты: не видно в событиях")
        print("-" * 70)
        # Ответ рендерится приложением в HTML; для консоли снимаем разметку грубо.
        html = result.get("answer_html") or ""
        import re

        text = re.sub(r"<[^>]+>", "", html)
        print(text.strip())
        return 0

    print("Не дождались ответа")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
