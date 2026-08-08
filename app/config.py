"""Настройки приложения. Всё приходит из окружения, значений в коде нет."""

import os

OUROBOROS_URL = os.environ.get("OUROBOROS_URL", "http://ouroboros:8765").rstrip("/")

# Сколько секунд рантайм ведёт задачу до принудительного мягкого завершения.
# В самом Ouroboros дедлайна по умолчанию нет вовсе (0), поэтому задаём явно.
TASK_TIMEOUT_SEC = int(os.environ.get("TASK_TIMEOUT_SEC", "600"))

# Сколько задач тянуть из рантайма, чтобы собрать историю сессии. Фильтровать
# по session_id приходится на нашей стороне: запроса «отдай задачи такой-то
# сессии» в API нет. Потолок самого рантайма — 500, выше не поднять.
HISTORY_LIMIT = min(int(os.environ.get("HISTORY_LIMIT", "200")), 500)

# Сетевой пароль нужен только если порт 8765 вынесен за localhost: обращение
# app → ouroboros идёт по сети Docker, то есть не с loopback, и попадает под
# проверку. При пустом пароле проверка пропускает всех.
NETWORK_PASSWORD = os.environ.get("OUROBOROS_NETWORK_PASSWORD", "").strip()
