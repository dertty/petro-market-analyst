"""Запись параметров проекта в data/settings.json перед стартом рантайма.

Зачем именно запись, а не надежда на окружение. В Ouroboros окружение и
settings.json — не два слоя с приоритетом, а один канал с защёлкой: значение из
окружения применяется ТОЛЬКО к ключам, которых в файле нет или значение пусто
(`config.py:1343-1347`). При этом любое owner-действие в веб-интерфейсе — не
только сохранение ключа, а вообще любое — идёт через read-modify-write:
`_owner_read_settings_raw()` сливает эталонные дефолты с файлом
(`merged = dict(defaults); merged.update(файл)`) и окружение не смотрит вовсе,
после чего на диск пишутся все полторы сотни ключей.

Отсюда правило: значение, лежащее в файле, такое сохранение ПЕРЕЖИВАЕТ — файл
побеждает дефолты. А значение, жившее только в .env, подменяется вендорским
дефолтом молча. Ровно так проект однажды получил gpt-5.2 в слоте web_search.

Поэтому источником истины остаётся .env (он документирован в .env.example и
лежит в git), а этот скрипт проецирует его на диск, делая значения устойчивыми
к веб-интерфейсу. Скрипт идемпотентен, отсутствие файла — не ошибка.

Запускается ДО рантайма (`depends_on: service_completed_successfully`): часть
ключей читается на старте сервера.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

SETTINGS_PATH = pathlib.Path(
    os.environ.get("OUROBOROS_SETTINGS_PATH", "/workspace/data/settings.json")
)

# Ключи, которые окружение само на диск не донесёт. Причин ровно две, и они разные.
#
# ПЕРВАЯ — вендорский дефолт НЕПУСТОЙ. Тогда read-modify-write из веб-интерфейса
# подменит наше значение своим. Это весь список ниже, кроме последней строки.
#
# ПРИ ДОБАВЛЕНИИ ПЕРЕМЕННОЙ РАНТАЙМА В COMPOSE ДОПИСАТЬ СЮДА: пропущенный ключ
# однажды подменится вендорским дефолтом, и признака этого не будет.
#
# Секретов по этой причине здесь нет намеренно: у ключей вроде OPENROUTER_API_KEY
# вендорский дефолт пустой, поэтому окружение авторствует их и после перезаписи
# файла — класть их на диск незачем.
#
# ВТОРАЯ — ключа нет в вендорском реестре ВООБЩЕ. Цикл наложения окружения идёт по
# `for key in SETTINGS_DEFAULTS` (config.py:1336), поэтому чужой ключ он не видит и
# не применяет никогда — ни на пустом значении, ни на пустом файле. Для таких ключей
# запись сюда не «страховка от перезаписи», а ЕДИНСТВЕННЫЙ способ доставки; без неё
# ключ из .env не доедет до рантайма вовсе. Так устроен GDELT_API_KEY: его читает
# скилл gdelt-mcp через PluginAPI.get_settings, то есть из settings.json.
#
# Побочный эффект второго случая: попав в settings.json, такой ключ становится
# «запрошенным» и требует owner-гранта скиллу, иначе тот уходит в missing_grants.
# Грант выдаёт scripts/enable_own_skills.py — он стартует после этого сервиса.
PROJECTED_FROM_ENV = [
    "OUROBOROS_RUNTIME_MODE",
    "TOTAL_BUDGET",
    "OUROBOROS_MODEL",
    "OUROBOROS_MODEL_LIGHT",
    "OUROBOROS_MODEL_FALLBACKS",
    "OUROBOROS_MODEL_DEEP_SELF_REVIEW",
    "OUROBOROS_REVIEW_MODELS",
    "OUROBOROS_EFFORT_REVIEW",
    "OUROBOROS_WEBSEARCH_MODEL",
    "OUROBOROS_SKILL_REVIEW_JOB_STALE_SEC",
    "GDELT_API_KEY",
]

# Ключи, которые окружение задать не может ВООБЩЕ: рантайм объявил их
# owner-защёлками, авторуемыми только с диска (`config._DISK_AUTHORED_SETTINGS`).
# Запись сюда — единственный способ задать их воспроизводимо.
#
# OUROBOROS_CONTEXT_MODE: вендорский дефолт — "max", а нам нужен "low" (иначе на
# чистой установке контекст раздувается вместе со счётом). Само по себе значение
# не поднимается: рантайм умеет только понижать max -> low, обратный переход
# требует явного действия владельца.
#
# OUROBOROS_CONTEXT_MODE_AUTO_LOW и OUROBOROS_SAFETY_MODE не трогаем: у первого
# есть рантаймовая защёлка против очистки, второй — про безопасность, и оба
# должны оставаться решением человека, а не побочным эффектом запуска.
DISK_AUTHORED = {
    "OUROBOROS_CONTEXT_MODE": "low",
}

# Журнал этого сервиса целиком виден в `docker compose logs`, поэтому значения
# ключей с такими окончаниями в отчёт не попадают — только факт записи.
SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_PASSWORD")


def report_line(key: str, value: str, previous) -> str:
    """Строка отчёта о записанном параметре, с маскировкой секретов."""
    if key.endswith(SECRET_SUFFIXES):
        was = "не был задан" if previous == "—" else "было другое"
        return f"  {key} = <скрыто> ({was})"
    return f"  {key} = {value!r} (было {previous!r})"


def main() -> int:
    settings: dict = {}
    if SETTINGS_PATH.exists():
        try:
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"ОШИБКА: не прочитать {SETTINGS_PATH}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(loaded, dict):
            print(f"ОШИБКА: {SETTINGS_PATH} — не объект JSON", file=sys.stderr)
            return 1
        settings = loaded

    desired: dict = dict(DISK_AUTHORED)
    for key in PROJECTED_FROM_ENV:
        value = str(os.environ.get(key, "") or "").strip()
        # Пустое значение означает «проект этот слот не задаёт» — тогда пусть
        # действует вендорский дефолт, записывать пустоту в файл незачем.
        if value:
            desired[key] = value

    changed = {k: v for k, v in desired.items() if settings.get(k) != v}
    if not changed:
        print(f"Все {len(desired)} параметров уже на месте — писать нечего.")
        return 0

    previous = {k: settings.get(k, "—") for k in changed}
    settings.update(desired)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, SETTINGS_PATH)
    except OSError as exc:
        print(f"ОШИБКА: не записать {SETTINGS_PATH}: {exc}", file=sys.stderr)
        return 1

    print(f"Записано параметров: {len(changed)} из {len(desired)}")
    for key, value in changed.items():
        print(report_line(key, value, previous[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
