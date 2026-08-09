"""Включение собственных и маркетплейс-скиллов проекта при старте.

Чтобы агент увидел скилл, в Ouroboros должны выполниться три независимых условия:
вердикт ревью на текущий хеш содержимого, гранты владельца на запрошенные скиллом
ключи и разрешения, и флаг «включено». Ничто из этого не делается само: автоматика
рантайма при старте штампует вердикт только засеянным native-скиллам, да и там
автовключение недоступно всему, что объявляет `net`.

Скрипт закрывает все три условия штатными owner-эндпоинтами:

  POST /api/owner/skills/{skill}/attest-review — owner-аттестация «Skip review».
      Пропускает дорогую фазу с ИИ-моделями для СВОИХ скиллов; детерминированный
      preflight при этом всё равно выполняется, и при его провале приходит 409.
  POST /api/skills/{skill}/grants {"items": [...]} — гранты владельца. Ключ
      становится «запрошенным» ровно тогда, когда он есть в settings.json: пока
      его там нет, скилл не просит ничего и грант не нужен. Грант привязан к хешу
      СОДЕРЖИМОГО скилла, поэтому обновление скилла его сбрасывает.
  POST /api/skills/{skill}/toggle {"enabled": true} — собственно включение.
      Без гранта на этом шаге приходит 409 «cannot enable until requested key and
      permission grants are approved», поэтому гранты выдаются строго раньше.

Обходятся каталоги внутри ./skills — это бинд-маунт бакета external, то есть
скиллы этого проекта, — плюс фиксированный список MARKETPLACE_SKILLS (скиллы
OuroborosHub, установленные вручную через UI). У них ревью уже пройдено ИИ-моделями
при установке и лежит в гитигнорируемом data/state/skills/, поэтому owner-
аттестация им не нужна и не применяется — скрипт восстанавливает только грант и
флаг «включено», если они слетели вместе с остальным состоянием в data/. Это не
теория: gdelt-mcp просит GDELT_API_KEY, который settings-seed кладёт в
settings.json на каждом старте, так что на чистом data/ без гранта он не
поднимется. Встроенные
(telegram, unix_computer_use) лежат в третьем бакете, скрипт их не видит, и
owner-аттестация к ним всё равно неприменима.

Идемпотентен: уже включённый скилл со свежим вердиктом пропускается.
"""

from __future__ import annotations

import os
import pathlib
import sys
import time
from typing import Dict, List, Optional

import requests

BASE_URL = os.environ.get("OUROBOROS_URL", "http://ouroboros:8765").rstrip("/")
NETWORK_PASSWORD = os.environ.get("OUROBOROS_NETWORK_PASSWORD", "").strip()
SKILLS_DIR = pathlib.Path(os.environ.get("OWN_SKILLS_DIR", "/workspace/skills"))

# Скиллы маркетплейса OuroborosHub. Установка (data/skills/ouroboroshub/<slug>) — вне
# git и делается вручную через UI; здесь фиксируется только желаемое состояние
# «включён», чтобы оно не терялось вместе с data/state/skills/ и не требовало
# каждый раз повторного клика владельца.
MARKETPLACE_SKILLS = ["gdelt-mcp", "keenable", "perplexity"]

# Рантайм поднимается за считаные секунды, но образ тяжёлый и на холодном старте
# Docker Desktop бывает медленным. Потолок с запасом.
READY_TIMEOUT_SEC = 180
READY_POLL_SEC = 2


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {NETWORK_PASSWORD}"} if NETWORK_PASSWORD else {}


def wait_for_runtime() -> bool:
    """Дождаться, пока рантайм начнёт отвечать. /api/state, а не /api/health.

    Второй отвечает и до того, как поднялся супервизор. Нам этого мало: реестр
    скиллов к тому моменту может быть ещё не собран.
    """
    deadline = time.monotonic() + READY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{BASE_URL}/api/state", headers=_headers(), timeout=10)
            if response.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(READY_POLL_SEC)
    return False


def own_skill_names() -> List[str]:
    """Каталоги в ./skills, похожие на скиллы. README и прочие файлы игнорируются."""
    if not SKILLS_DIR.is_dir():
        return []
    names = []
    for path in sorted(SKILLS_DIR.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        if (path / "SKILL.md").exists() or (path / "skill.json").exists():
            names.append(path.name)
    return names


def fetch_skill_state() -> Dict[str, dict]:
    response = requests.get(f"{BASE_URL}/api/extensions", headers=_headers(), timeout=30)
    response.raise_for_status()
    skills = response.json().get("skills") or []
    return {str(item.get("name")): item for item in skills if isinstance(item, dict)}


def attest(name: str) -> Optional[str]:
    """Owner-аттестация. Возвращает текст ошибки или None при успехе."""
    response = requests.post(
        f"{BASE_URL}/api/owner/skills/{name}/attest-review", headers=_headers(), timeout=120
    )
    payload = response.json() if response.content else {}
    if response.status_code != 200 or payload.get("status") != "clean":
        # 409 здесь означает провал детерминированного preflight — то есть в скилле
        # реальная поломка, а не отказ механизма аттестации.
        return payload.get("error") or f"HTTP {response.status_code}"
    return None


def grant(name: str, items: List[str]) -> Optional[str]:
    """Owner-грант на ключи и разрешения. Возвращает текст ошибки или None."""
    response = requests.post(
        f"{BASE_URL}/api/skills/{name}/grants",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"items": items},
        timeout=120,
    )
    if response.status_code != 200:
        payload = response.json() if response.content else {}
        return payload.get("error") or f"HTTP {response.status_code}"
    return None


def enable(name: str) -> Optional[str]:
    response = requests.post(
        f"{BASE_URL}/api/skills/{name}/toggle",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"enabled": True},
        timeout=120,
    )
    if response.status_code != 200:
        payload = response.json() if response.content else {}
        return payload.get("error") or f"HTTP {response.status_code}"
    return None


def ensure_review(name: str, entry: dict) -> Optional[str]:
    """Довести вердикт до свежего owner-аттестацией. Текст ошибки или None."""
    if not entry.get("owner_attestable"):
        return (
            "вердикт ревью отсутствует или устарел, а owner-аттестация для этого "
            "скилла недоступна — включите его вручную через интерфейс рантайма"
        )
    print(f"  {name}: подтверждаю код владельцем (LLM-ревью пропускается)")
    if error := attest(name):
        return f"аттестация не прошла: {error}"
    return None


def process(name: str, entry: Optional[dict], *, required: bool = True) -> Optional[str]:
    """Довести один скилл до включённого состояния. Текст ошибки или None.

    ``required=False`` — для маркетплейс-скиллов: их отсутствие в рантайме (не
    установлены в этом окружении) не ошибка, а нормальный пропуск.
    """
    if entry is None:
        if not required:
            print(f"  {name}: не установлен в этом окружении — пропускаю")
            return None
        return "рантайм не видит этот скилл — проверьте монтирование ./skills"
    if entry.get("load_error"):
        return f"скилл не загружается: {entry['load_error']}"

    review_ok = entry.get("review_status") == "clean" and not entry.get("review_stale")
    grants = entry.get("grants") or {}
    missing_grants = [
        *(grants.get("missing_keys") or []),
        *(grants.get("missing_permissions") or []),
    ]

    # Флага «включено» мало: при недостающем гранте рантайм оставляет его поднятым,
    # но само расширение выгружает (not live: missing_grants). Поэтому skip-условие
    # проверяет и гранты, иначе такой скилл молча оставался бы нерабочим.
    if entry.get("enabled") and review_ok and not missing_grants:
        print(f"  {name}: уже включён, вердикт свежий, гранты на месте — пропускаю")
        return None

    if not review_ok and (error := ensure_review(name, entry)):
        return error

    # Строго до включения: с недостающим грантом toggle отвечает 409.
    if missing_grants:
        print(f"  {name}: выдаю грант владельца на {', '.join(missing_grants)}")
        if error := grant(name, missing_grants):
            return f"грант не выдан: {error}"

    print(f"  {name}: включаю")
    if error := enable(name):
        return f"включение не прошло: {error}"
    return None


def process_batch(
    names: List[str], state: Dict[str, dict], *, required: bool
) -> Dict[str, str]:
    """Прогнать process() по списку скиллов, вернуть {имя: ошибка} для неудач."""
    failures: Dict[str, str] = {}
    for name in names:
        try:
            if error := process(name, state.get(name), required=required):
                failures[name] = error
        except requests.RequestException as exc:
            failures[name] = f"сетевая ошибка: {exc}"
    return failures


def main() -> int:
    names = own_skill_names()
    if not names and not MARKETPLACE_SKILLS:
        print(f"В {SKILLS_DIR} нет собственных скиллов — включать нечего.")
        return 0

    print(f"Собственные скиллы: {', '.join(names) or '—'}")
    print(f"Маркетплейс-скиллы: {', '.join(MARKETPLACE_SKILLS) or '—'}")
    if not wait_for_runtime():
        print(
            f"ОШИБКА: рантайм не ответил за {READY_TIMEOUT_SEC} с ({BASE_URL}). "
            "Скиллы остались выключенными.",
            file=sys.stderr,
        )
        return 1

    try:
        state = fetch_skill_state()
    except requests.RequestException as exc:
        print(f"ОШИБКА: не удалось получить список скиллов: {exc}", file=sys.stderr)
        return 1

    failures = process_batch(names, state, required=True)
    failures.update(process_batch(MARKETPLACE_SKILLS, state, required=False))

    if failures:
        print("\nНе удалось включить:", file=sys.stderr)
        for name, error in failures.items():
            print(f"  {name}: {error}", file=sys.stderr)
        return 1

    print(f"\nГотово: {len(names) + len(MARKETPLACE_SKILLS)} скилл(ов) обработано.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
