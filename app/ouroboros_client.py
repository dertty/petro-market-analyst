"""Тонкий клиент к task API Ouroboros.

Здесь только транспорт: сборка описания задачи живёт в prompt.py, отбор задач
сессии — в main.py.
"""

from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from app.config import HISTORY_LIMIT, NETWORK_PASSWORD, OUROBOROS_URL, TASK_TIMEOUT_SEC

# Инструменты, снятые с аналитической задачи. Список чёрный: белого списка в
# контракте задачи нет (allowed_resources ограничивает сеть, а не инструменты).
#
# Неизвестное имя молча ничего не отключает, поэтому каждое сверено с реестром
# v6.90.1. ПРИ ОБНОВЛЕНИИ ПОДМОДУЛЯ СПИСОК ПЕРЕСМОТРЕТЬ: новый инструмент записи
# ослабит защиту без единого признака.
DISABLED_TOOLS = [
    # Прямая запись файлов и оболочка.
    "write_file", "edit_text", "apply_patch", "edit_batch", "run_command",
    # Запись знаний и состояния проекта. На своём каталоге задачи запись всё равно
    # не попала бы в общую базу, а инструмент отрапортовал бы успехом.
    "knowledge_write", "journal_write", "workpad_write", "tree_note",
    # Жизненный цикл скиллов.
    "skill_review", "skill_preflight", "toggle_skill", "submit_skill_to_hub",
    # Самоизменение рантайма и его настроек.
    "update_identity", "update_scratchpad", "memory_update_registry",
    "toggle_evolution", "toggle_consciousness", "switch_model",
    "request_restart", "promote_to_stable", "set_tool_timeout", "enable_tools",
    # Git: коммиты, откаты и вся работа с PR.
    "commit_reviewed", "vcs_commit_reviewed", "vcs_restore",
    "vcs_revert", "vcs_rollback", "vcs_pull_ff",
    "fetch_pr_ref", "create_integration_branch", "cherry_pick_pr_commits",
    "stage_adaptations", "stage_pr_merge", "integrate_subagent_patch",
    # Порождение платных подзадач. delegate_start вдобавок тянет провижнинг
    # Claudexor — единственный реальный путь, по которому в контейнер приехали бы
    # те самые ~256 МБ.
    "delegate_start", "delegate_wait", "delegate_cancel", "schedule_subagent",
    # Управление чужими задачами.
    "steer_task", "cancel_task", "promote_chat_to_task", "route_to_project",
    # Исходящие эффекты наружу.
    "forward_to_worker", "send_user_message",
    "send_photo", "send_video", "send_file",
    "advisory_review", "run_ci_tests",
    # Браузер: сетевой egress. Бинарников в образе нет, но полагаться на их
    # отсутствие хрупко.
    "browse_page", "browser_action",
    "start_service", "stop_service",
    "create_github_issue", "close_github_issue",
    "comment_on_pr", "comment_on_issue",
]

# Каталоги кода, защищённые от записи. Пути ОБЯЗАТЕЛЬНО абсолютные: относительный
# достраивается не от корня проекта, а от workspace_root → repo_dir → ..., и у
# аналитика workspace_root пуст, так что "skills" превратилось бы в
# /workspace/vendor/ouroboros/skills — защита выглядела бы настроенной, но не работала.
PROTECTED_PATHS = [
    "/workspace/skills",
    "/workspace/vendor",
    "/workspace/app",
    "/workspace/scripts",
    # База отчётов и векторный индекс: задача читает их через инструмент поиска,
    # писать туда ей незачем. Пополнение корпуса идёт мимо агента — файлом в
    # reports/pdf и пересборкой индекса.
    "/workspace/reports",
]

_FINAL_STATUSES = {"completed", "failed", "cancelled", "timeout", "error"}


def is_final(status: str) -> bool:
    return str(status or "").lower() in _FINAL_STATUSES


def _headers() -> Dict[str, str]:
    if NETWORK_PASSWORD:
        return {"Authorization": f"Bearer {NETWORK_PASSWORD}"}
    return {}


def answer_text(task: Dict[str, Any]) -> str:
    """Текст ответа из результата задачи.

    Полный текст лежит в поле result верхнего уровня, его и берём. final_answer —
    лишь подстраховка: без answer_protocol="final_answer_line" в контракте задачи
    он пуст, а с протоколом содержал бы одну строку после маркера и заставлял
    модель дублировать короткие ответы строкой FINAL ANSWER. Поля final_text в
    отдаваемом результате нет — оно внутри loop_outcome.
    """
    return str(task.get("result") or task.get("final_answer") or "").strip()


class OuroborosError(RuntimeError):
    """Рантайм недоступен или ответил отказом — с текстом для показа человеку."""


class OuroborosClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._http = client

    async def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            response = await self._http.request(
                method, f"{OUROBOROS_URL}{path}", headers=_headers(), **kwargs
            )
        except httpx.RequestError as exc:
            raise OuroborosError(
                "Сервис Ouroboros не отвечает. Проверьте, что контейнер поднят: "
                "docker compose ps"
            ) from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            raise OuroborosError(f"Ouroboros ответил {response.status_code}: {detail}")
        return response.json()

    async def state(self) -> Dict[str, Any]:
        """Готовность рантайма принимать задачи.

        Именно /api/state, а не /api/health: последний отвечает и тогда, когда
        супервизор ещё не поднят и задачи брать некому.
        """
        return await self._request("GET", "/api/state", timeout=10)

    async def create_task(self, description: str, question: str, session_id: str) -> str:
        body = {
            "description": description,
            "timeout_sec": TASK_TIMEOUT_SEC,
            # Свой каталог данных на задачу: пустой журнал переписки, скопированная
            # база знаний. Так между сессиями и пользователями не течёт ничего.
            "memory_mode": "forked",
            # Рантайм сам положит его в metadata; переданный клиентом
            # metadata.session_id вырезается как зарезервированный ключ.
            "session_id": session_id,
            "metadata": {"question": question},
            "disabled_tools": DISABLED_TOOLS,
            "resource_policy": {
                "protected_artifacts": [{"id": "code", "paths": PROTECTED_PATHS}]
            },
        }
        created = await self._request("POST", "/api/tasks", json=body, timeout=60)
        task_id = str(created.get("task_id") or "")
        if not task_id:
            raise OuroborosError("Ouroboros не вернул идентификатор задачи")
        return task_id

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/tasks/{task_id}", timeout=30)

    async def list_tasks(self) -> List[Dict[str, Any]]:
        data = await self._request(
            "GET", "/api/tasks", params={"limit": HISTORY_LIMIT}, timeout=30
        )
        tasks = data.get("tasks")
        return tasks if isinstance(tasks, list) else []

    async def cancel_task(self, task_id: str) -> Dict[str, Any]:
        return await self._request("POST", f"/api/tasks/{task_id}/cancel", json={}, timeout=30)

    async def stream_events(
        self, task_id: str, cursor: int, wait: int = 30
    ) -> AsyncIterator[bytes]:
        """Поток событий задачи как есть, байт в байт.

        Курсор передаётся апстриму: браузер при переподключении присылает
        Last-Event-ID, и это позволяет догрузить события с нужного места.
        """
        url = f"{OUROBOROS_URL}/api/tasks/{task_id}/events"
        params = {"cursor": cursor, "wait": wait}
        async with self._http.stream(
            "GET", url, params=params, headers=_headers(), timeout=wait + 30
        ) as response:
            async for chunk in response.aiter_raw():
                yield chunk


def session_of(task: Dict[str, Any]) -> str:
    """session_id задачи. Лежит полем верхнего уровня, но подстраховываемся metadata."""
    direct = str(task.get("session_id") or "").strip()
    if direct:
        return direct
    metadata = task.get("metadata")
    if isinstance(metadata, dict):
        return str(metadata.get("session_id") or "").strip()
    return ""


def question_of(task: Dict[str, Any]) -> str:
    """Вопрос пользователя из metadata — иначе в списке был бы весь шаблон описания."""
    metadata = task.get("metadata")
    if isinstance(metadata, dict):
        return str(metadata.get("question") or "").strip()
    return ""


def cost_of(task: Dict[str, Any]) -> Optional[float]:
    """Стоимость задачи. Плоское поле результата, а не usage.cost_usd."""
    value = task.get("cost_usd")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
