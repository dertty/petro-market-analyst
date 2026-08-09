"""Исторический ряд спотовых цен: EIA API v2, кэш на три часа, снапшот как запасной путь.

Ряд сводится к недельной сетке (последнее наблюдение недели) ещё здесь, до всякой
модели. Причина — не удобство, а горизонты: 12 месяцев это 52 недельных шага, что
укладывается в один проход Chronos-Bolt, тогда как 250 дневных шагов потребовали бы
авторегрессионной раскрутки с накоплением ошибки.

Здесь только стандартная библиотека плюс requests: numpy и pandas нужны методам
прогноза, а загрузке данных — нет.
"""

from __future__ import annotations

import csv
import json
import pathlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

EIA_ENDPOINT = "https://api.eia.gov/v2/petroleum/pri/spt/data/"

# Сколько наблюдений просим у EIA. Потолок ответа API — 5000 строк, дневной ряд
# Brent с 1987 года длиннее, поэтому берём срез с конца: сортировка по периоду
# по убыванию отдаёт последние ~20 лет, а больше контекста моделям и не нужно.
EIA_PAGE_SIZE = 5000

# Кэш считается годным три часа. Сутки были рассуждением о частоте публикации: серия
# EIA обновляется редко, значит и ходить за ней часто незачем. На практике это добавило
# к чужому лагу свой собственный: 2026-08-10 прогноз считался по ряду, загруженному
# накануне в 16:39, — к возрасту данных прибавились ещё сутки, и ровно на этой неделе
# рынок двигался на 8%. Свой лаг убираем: он единственный, которым мы управляем.
# Лишние походы в API дёшевы (ключ бесплатный, ответ кэшируется), молчаливо
# устаревший якорь — нет.
CACHE_TTL_SEC = 3 * 3600

BENCHMARKS: Dict[str, Dict[str, str]] = {
    "brent": {
        "eia_series": "RBRTE",
        "title": "Brent, спот FOB (Европа)",
        "source": "EIA, серия RBRTE (Europe Brent Spot Price FOB), дневная",
    },
    "wti": {
        "eia_series": "RWTC",
        "title": "WTI, спот FOB (Cushing, Оклахома)",
        "source": "EIA, серия RWTC (Cushing OK WTI Spot Price FOB), дневная",
    },
}


class DataUnavailable(RuntimeError):
    """Ряд не удалось получить ни из API, ни из кэша, ни из снапшота."""


@dataclass
class Series:
    """Недельный ряд цен и происхождение чисел.

    origin различает три пути не ради статистики: снапшот протухает, и ответ, собранный
    на нём, обязан нести об этом пометку — иначе аналитик процитирует старую цену как
    текущую.
    """

    benchmark: str
    dates: List[str]
    values: List[float]
    origin: str  # eia_api | cache | snapshot
    source: str
    fetched_at: Optional[str] = None

    @property
    def last_date(self) -> str:
        return self.dates[-1]

    @property
    def last_value(self) -> float:
        return self.values[-1]

    @property
    def age_days(self) -> int:
        """Сколько дней последнему наблюдению ряда на момент запроса.

        Число, а не примечание. Лаг публикации EIA около недели — это норма, и слово
        «свежие» её не отличает от протухшего кэша или снапшота полугодовой давности;
        отличает только величина. Аналитику нужен именно возраст: он решает, можно ли
        подавать якорь как сегодняшнюю цену (обычно нельзя) и насколько рынок мог
        уйти с тех пор.
        """
        return (datetime.now(timezone.utc).date() - date.fromisoformat(self.last_date)).days

    def origin_note(self) -> str:
        if self.origin == "eia_api":
            return "данные получены из API EIA"
        if self.origin == "cache":
            return f"данные из кэша (загружены {self.fetched_at})"
        return (
            "ВНИМАНИЕ: API EIA недоступен, использован снапшот из поставки скилла — "
            "ряд может быть устаревшим, последнюю цену перепроверить отдельно"
        )


def _to_weekly(observations: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """Последнее наблюдение каждой ISO-недели.

    Дата остаётся датой самого наблюдения, а не концом недели: подписывать ценой
    пятницы то, что снято в среду, значит врать о моменте среза.
    """
    weekly: Dict[Tuple[int, int], Tuple[str, float]] = {}
    for day, value in sorted(observations):
        iso = date.fromisoformat(day).isocalendar()
        weekly[(iso[0], iso[1])] = (day, value)
    return [weekly[key] for key in sorted(weekly)]


def _fetch_eia(series_id: str, api_key: str, timeout: int = 30) -> List[Tuple[str, float]]:
    params = [
        ("api_key", api_key),
        ("frequency", "daily"),
        ("data[0]", "value"),
        ("facets[series][]", series_id),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "desc"),
        ("length", str(EIA_PAGE_SIZE)),
    ]
    response = requests.get(EIA_ENDPOINT, params=params, timeout=timeout)
    response.raise_for_status()
    rows = (response.json().get("response") or {}).get("data") or []
    observations: List[Tuple[str, float]] = []
    for row in rows:
        period = str(row.get("period") or "").strip()
        value = row.get("value")
        # EIA отдаёт пропуски биржевых выходных как null — это не ошибка ответа,
        # такие строки просто выбрасываем.
        if not period or value is None:
            continue
        try:
            observations.append((period, float(value)))
        except (TypeError, ValueError):
            continue
    if not observations:
        raise DataUnavailable(f"EIA вернул пустой ряд для {series_id}")
    return observations


def _cache_path(state_dir: pathlib.Path, benchmark: str) -> pathlib.Path:
    return pathlib.Path(state_dir) / "series" / f"{benchmark}.json"


def _read_cache(path: pathlib.Path) -> Optional[Dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_age_sec(payload: Dict) -> float:
    try:
        fetched = datetime.fromisoformat(str(payload["fetched_at"]))
    except (KeyError, TypeError, ValueError):
        return float("inf")
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - fetched).total_seconds()


def _write_cache(path: pathlib.Path, observations: List[Tuple[str, float]]) -> str:
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {"fetched_at": fetched_at, "weekly": [list(item) for item in observations]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        # Кэш — оптимизация, а не условие работы: не пишется, значит следующий вызов
        # просто снова сходит в API.
        pass
    return fetched_at


def _read_snapshot(skill_dir: pathlib.Path, benchmark: str) -> List[Tuple[str, float]]:
    path = pathlib.Path(skill_dir) / "data" / f"{benchmark}_weekly.csv"
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        raise DataUnavailable(f"снапшот {path.name} недоступен: {exc}") from exc
    observations: List[Tuple[str, float]] = []
    for row in rows:
        try:
            observations.append((str(row["date"]).strip(), float(row["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not observations:
        raise DataUnavailable(f"снапшот {path.name} пуст или повреждён")
    return sorted(observations)


def load_weekly(
    benchmark: str,
    skill_dir: pathlib.Path,
    state_dir: pathlib.Path,
    api_key: Optional[str],
) -> Series:
    """Недельный ряд: API EIA, при отказе — кэш, при его отсутствии — снапшот."""
    if benchmark not in BENCHMARKS:
        raise DataUnavailable(
            f"неизвестный эталон '{benchmark}', доступны: {', '.join(sorted(BENCHMARKS))}"
        )
    meta = BENCHMARKS[benchmark]
    cache_file = _cache_path(state_dir, benchmark)
    cached = _read_cache(cache_file)

    if cached and _cache_age_sec(cached) < CACHE_TTL_SEC:
        weekly = [(str(day), float(value)) for day, value in cached.get("weekly", [])]
        if weekly:
            return Series(
                benchmark=benchmark,
                dates=[day for day, _ in weekly],
                values=[value for _, value in weekly],
                origin="cache",
                source=meta["source"],
                fetched_at=str(cached.get("fetched_at") or ""),
            )

    if api_key:
        try:
            weekly = _to_weekly(_fetch_eia(meta["eia_series"], api_key))
            fetched_at = _write_cache(cache_file, weekly)
            return Series(
                benchmark=benchmark,
                dates=[day for day, _ in weekly],
                values=[value for _, value in weekly],
                origin="eia_api",
                source=meta["source"],
                fetched_at=fetched_at,
            )
        except (requests.RequestException, DataUnavailable, ValueError):
            # Молча падать в снапшот нельзя было бы, если бы ответ не нёс origin;
            # он его несёт, поэтому причина отказа доедет до аналитика пометкой.
            pass

    weekly = _read_snapshot(skill_dir, benchmark)
    return Series(
        benchmark=benchmark,
        dates=[day for day, _ in weekly],
        values=[value for _, value in weekly],
        origin="snapshot",
        source=meta["source"] + " — снапшот из поставки скилла",
    )
