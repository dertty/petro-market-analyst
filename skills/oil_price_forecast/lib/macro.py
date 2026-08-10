"""Макрофон для нефти: семь структурированных рядов из FRED и сервисов ЦБ РФ.

Здесь нет ни модели, ни LLM — только загрузка чисел, арифметика дельт и кэш
(CACHE_TTL_SEC общий с data.py — три часа).
Ряды намеренно только структурированные (CSV/XML из публичных API без ключей): отчёты,
пресс-релизы и прочий неструктурированный текст агент читает сам через поиск и RAG,
инструменту они запрещены по условию задачи.

Каждая серия качается независимо, и это принципиально: сервисы ЦБ РФ бывают медленными
или троттлят зарубежные IP, и один мёртвый источник не должен ронять весь макрофон.
Упавшая серия берётся из прошлого кэша с пометкой stale_cache и датой загрузки — несвежее
число, выданное без пометки, аналитик процитировал бы как текущее.

Источники (проверены живьём 2026-08-09):
  * FRED, fredgraph.csv — без ключа. Заголовок CSV уже переименовывали
    (DATE → observation_date), поэтому парсер его пропускает, не проверяя текст.
    Пропуски значений приходят точкой «.».
  * ЦБ РФ, XML_dynamic.asp — официальный курс. XML в windows-1251 (requests его
    декодирует по заголовку ответа), десятичные запятые, курс = Value / Nominal.
  * ЦБ РФ, DailyInfoWebServ (SOAP) — ключевая ставка. Более современного
    структурированного эндпоинта у ЦБ нет; сервис легаси и может быть закрыт,
    на этот случай и существует stale-фолбэк.
"""

from __future__ import annotations

import json
import pathlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

from .data import CACHE_TTL_SEC

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
CBR_FX_URL = "https://www.cbr.ru/scripts/XML_dynamic.asp"
CBR_KEYRATE_URL = "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx"
CBR_USD_ID = "R01235"

# Окно загрузки. Для годовой дельты нужен год с запасом; для CPI год-к-году — ещё
# 12 месяцев истории поверх, поэтому берём три года и не плодим два разных окна.
FETCH_WINDOW_DAYS = 3 * 365

# Дельты считаются к последней точке не позже чем (последняя дата − столько дней).
CHANGE_WINDOWS = {"change_1m": 30, "change_1y": 365}

_KEYRATE_SOAP_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
    'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    "<soap:Body>"
    '<KeyRate xmlns="http://web.cbr.ru/">'
    "<fromDate>{from_date}</fromDate><ToDate>{to_date}</ToDate>"
    "</KeyRate></soap:Body></soap:Envelope>"
)

# Реестр серий. oil_note — статичная подсказка о знаке влияния: она едет в ответ,
# чтобы агент не сочинял направление связи заново при каждом вызове.
MACRO_SERIES: Dict[str, Dict[str, str]] = {
    "usd_index": {
        "block": "dollar",
        "fetch": "fred",
        "fred_id": "DTWEXBGS",
        "title": "Индекс доллара (broad, DTWEXBGS)",
        "units": "индекс",
        "change_units": "%",
        "source": "FRED (ФРБ Сент-Луиса), серия DTWEXBGS, дневная",
        "oil_note": (
            "нефть номинирована в долларах: укрепление доллара — фактор давления "
            "на цену вниз; связь статистическая, не механическая"
        ),
    },
    "fed_funds": {
        "block": "dollar",
        "fetch": "fred",
        "fred_id": "DFF",
        "title": "Ставка ФРС (effective federal funds)",
        "units": "% годовых",
        "change_units": "п.п.",
        "source": "FRED, серия DFF, дневная",
        "oil_note": (
            "жёстче ДКП — дороже финансирование запасов и слабее ожидания спроса; "
            "знак влияния на нефть нестабилен на коротких окнах"
        ),
    },
    "us_cpi_yoy": {
        "block": "dollar",
        "fetch": "fred_yoy",
        "fred_id": "CPIAUCSL",
        "title": "Инфляция США (CPI, год к году)",
        "units": "% г/г",
        "change_units": "п.п.",
        "source": "рассчитано из CPIAUCSL (FRED, первоисточник BLS), месячная",
        "oil_note": (
            "фон для решений ФРС; причинность двусторонняя — нефть сама входит в CPI"
        ),
    },
    "us_breakeven_10y": {
        "block": "dollar",
        "fetch": "fred",
        "fred_id": "T10YIE",
        "title": "Инфляционные ожидания США (10-летний breakeven)",
        "units": "%",
        "change_units": "п.п.",
        "source": "FRED, серия T10YIE, дневная",
        "oil_note": "рыночные ожидания инфляции; рост часто сопровождает рост сырья",
    },
    "global_activity": {
        "block": "global_demand",
        "fetch": "fred",
        "fred_id": "IGREA",
        "title": "Индекс глобальной реальной активности Килиана (IGREA)",
        "units": "пункты (отклонение от тренда)",
        "change_units": "пункты",
        "source": "FRED, серия IGREA (Kilian, по ставкам фрахта), месячная",
        "oil_note": (
            "стандартный прокси мирового спроса на нефть; важна динамика, не уровень"
        ),
    },
    "usd_rub": {
        "block": "russia",
        "fetch": "cbr_fx",
        "title": "Курс USD/RUB (официальный ЦБ РФ)",
        "units": "руб./долл.",
        "change_units": "%",
        "source": "ЦБ РФ, XML_dynamic (официальный курс, не биржевой)",
        "oil_note": (
            "на мировую цену нефти не влияет; определяет рублёвую цену барреля "
            "и доходы бюджета РФ"
        ),
    },
    "cbr_key_rate": {
        "block": "russia",
        "fetch": "cbr_key_rate",
        "title": "Ключевая ставка ЦБ РФ",
        "units": "% годовых",
        "change_units": "п.п.",
        "source": "ЦБ РФ, DailyInfoWebServ/KeyRate",
        "oil_note": (
            "на мировую цену нефти не влияет; фон для рубля и рублёвой стоимости "
            "финансирования"
        ),
    },
}

BLOCK_ORDER = ("dollar", "global_demand", "russia")
BLOCK_TITLES = {
    "dollar": "Долларовый блок",
    "global_demand": "Мировой спрос",
    "russia": "Российский блок",
}


class MacroUnavailable(RuntimeError):
    """Серию не удалось получить ни из источника, ни из кэша."""


def _fetch_fred_csv(series_id: str, timeout: int = 30) -> List[Tuple[str, float]]:
    start = (datetime.now(timezone.utc) - timedelta(days=FETCH_WINDOW_DAYS)).date()
    response = requests.get(
        FRED_CSV_URL, params={"id": series_id, "cosd": start.isoformat()}, timeout=timeout
    )
    response.raise_for_status()
    observations: List[Tuple[str, float]] = []
    for line in response.text.splitlines()[1:]:  # заголовок пропускаем, не проверяя
        parts = line.split(",")
        if len(parts) != 2:
            continue
        day, raw = parts[0].strip(), parts[1].strip()
        if not day or raw in ("", "."):
            continue
        try:
            observations.append((day, float(raw)))
        except ValueError:
            continue
    if not observations:
        raise MacroUnavailable(f"FRED вернул пустой ряд для {series_id}")
    return sorted(observations)


def _yoy(observations: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """Год к году по месячному ряду: сравнение с точкой ровно 12 наблюдений назад."""
    result: List[Tuple[str, float]] = []
    for i in range(12, len(observations)):
        day, value = observations[i]
        _, base = observations[i - 12]
        if base:
            result.append((day, (value / base - 1.0) * 100.0))
    if not result:
        raise MacroUnavailable("для расчёта год-к-году не хватает истории")
    return result


def _cbr_date_range() -> Tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=FETCH_WINDOW_DAYS)
    return start.strftime("%d/%m/%Y"), today.strftime("%d/%m/%Y")


def _fetch_cbr_fx(timeout: int = 30) -> List[Tuple[str, float]]:
    date_from, date_to = _cbr_date_range()
    response = requests.get(
        CBR_FX_URL,
        params={"date_req1": date_from, "date_req2": date_to, "VAL_NM_RQ": CBR_USD_ID},
        timeout=timeout,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    observations: List[Tuple[str, float]] = []
    for record in root.findall(".//Record"):
        day = (record.get("Date") or "").strip()  # DD.MM.YYYY
        value_node = record.find("Value")
        nominal_node = record.find("Nominal")
        if not day or value_node is None or value_node.text is None:
            continue
        try:
            value = float(value_node.text.replace(",", "."))
            nominal = float((nominal_node.text or "1").replace(",", ".")) if nominal_node is not None else 1.0
            iso = datetime.strptime(day, "%d.%m.%Y").date().isoformat()
            observations.append((iso, value / nominal))
        except ValueError:
            continue
    if not observations:
        raise MacroUnavailable("ЦБ РФ вернул пустой ряд курса USD/RUB")
    return sorted(observations)


def _fetch_cbr_key_rate(timeout: int = 30) -> List[Tuple[str, float]]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=FETCH_WINDOW_DAYS)
    body = _KEYRATE_SOAP_BODY.format(from_date=start.isoformat(), to_date=today.isoformat())
    response = requests.post(
        CBR_KEYRATE_URL,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "http://web.cbr.ru/KeyRate",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    observations: List[Tuple[str, float]] = []
    for record in root.iter():
        if not record.tag.endswith("KR"):
            continue
        day = rate = None
        for child in record:
            if child.tag.endswith("DT"):
                day = (child.text or "")[:10]
            elif child.tag.endswith("Rate"):
                rate = child.text
        if not day or rate is None:
            continue
        try:
            observations.append((day, float(rate.replace(",", "."))))
        except ValueError:
            continue
    if not observations:
        raise MacroUnavailable("ЦБ РФ вернул пустой ряд ключевой ставки")
    return sorted(observations)


_FETCHERS = {
    "fred": lambda meta: _fetch_fred_csv(meta["fred_id"]),
    "fred_yoy": lambda meta: _yoy(_fetch_fred_csv(meta["fred_id"])),
    "cbr_fx": lambda meta: _fetch_cbr_fx(),
    "cbr_key_rate": lambda meta: _fetch_cbr_key_rate(),
}


def _change(observations: List[Tuple[str, float]], days: int, as_pct: bool) -> Optional[float]:
    """Дельта последнего значения к последней точке не позже (последняя дата − days).

    Точной базовой даты в ряду может не быть (выходные, месячная сетка), поэтому
    берётся ближайшая доступная точка слева — так дельта честно считается по тем
    наблюдениям, которые есть, а не интерполируется.
    """
    last_day, last_value = observations[-1]
    cutoff = (datetime.fromisoformat(last_day) - timedelta(days=days)).date().isoformat()
    base: Optional[float] = None
    for day, value in observations:
        if day <= cutoff:
            base = value
        else:
            break
    if base is None:
        return None
    if as_pct:
        return round((last_value / base - 1.0) * 100.0, 2) if base else None
    return round(last_value - base, 2)


def _build_entry(key: str, observations: List[Tuple[str, float]], fetched_at: str) -> Dict:
    meta = MACRO_SERIES[key]
    as_pct = meta["change_units"] == "%"
    last_day, last_value = observations[-1]
    entry = {
        "id": key,
        "title": meta["title"],
        "units": meta["units"],
        "latest": {"date": last_day, "value": round(last_value, 2)},
        "change_units": meta["change_units"],
        "source": meta["source"],
        "oil_note": meta["oil_note"],
        "fetched_at": fetched_at,
    }
    for name, days in CHANGE_WINDOWS.items():
        entry[name] = _change(observations, days, as_pct)
    return entry


def _origin_note(key: str, origin: str, fetched_at: str) -> str:
    notes = {
        "fresh": "данные получены из источника",
        "cache": f"данные из кэша (загружены {fetched_at})",
        "stale_cache": (
            f"ВНИМАНИЕ: источник недоступен, данные из устаревшего кэша "
            f"(загружены {fetched_at}) — свежее значение перепроверить отдельно"
        ),
    }
    note = notes[origin]
    if key == "global_activity":
        # Лаг публикации ~2 месяца — свойство самого индекса, а не сбой загрузки.
        note += "; IGREA публикуется с лагом около двух месяцев, это норма"
    return note


def _cache_path(state_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(state_dir) / "series" / "macro.json"


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


def _blocks(entries: Dict[str, Dict]) -> Dict[str, List[Dict]]:
    blocks: Dict[str, List[Dict]] = {block: [] for block in BLOCK_ORDER}
    for key in MACRO_SERIES:  # порядок реестра, а не порядок словаря кэша
        if key in entries:
            blocks[MACRO_SERIES[key]["block"]].append(entries[key])
    return blocks


def load_macro(state_dir: pathlib.Path) -> Dict:
    """Макрофон: свежий кэш отдаётся целиком, иначе — посерийное обновление с фолбэком."""
    cache_file = _cache_path(state_dir)
    cached = _read_cache(cache_file)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if cached and _cache_age_sec(cached) < CACHE_TTL_SEC:
        entries: Dict[str, Dict] = {}
        for key, entry in (cached.get("series") or {}).items():
            if key not in MACRO_SERIES:
                continue
            entry = dict(entry)
            # Серия могла попасть в кэш уже несвежей — тогда пометка сохраняется.
            origin = "stale_cache" if entry.get("origin") == "stale_cache" else "cache"
            entry["origin"] = origin
            entry["origin_note"] = _origin_note(key, origin, str(entry.get("fetched_at")))
            entries[key] = entry
        if entries:
            return {"as_of": now, "blocks": _blocks(entries), "errors": dict(cached.get("errors") or {})}

    old_entries = (cached.get("series") or {}) if cached else {}
    entries = {}
    errors: Dict[str, str] = {}
    for key, meta in MACRO_SERIES.items():
        try:
            observations = _FETCHERS[meta["fetch"]](meta)
            entry = _build_entry(key, observations, fetched_at=now)
            entry["origin"] = "fresh"
            entry["origin_note"] = _origin_note(key, "fresh", now)
            entries[key] = entry
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            stale = old_entries.get(key)
            if stale:
                entry = dict(stale)
                entry["origin"] = "stale_cache"
                entry["origin_note"] = _origin_note(
                    key, "stale_cache", str(entry.get("fetched_at"))
                )
                entries[key] = entry
            errors[key] = reason

    if not entries:
        raise MacroUnavailable(
            "ни одна макро-серия не получена: " + "; ".join(errors.values())
        )

    payload = {"fetched_at": now, "series": entries, "errors": errors}
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # Кэш — оптимизация, а не условие работы (как в data.py).
        pass

    return {"as_of": now, "blocks": _blocks(entries), "errors": errors}
