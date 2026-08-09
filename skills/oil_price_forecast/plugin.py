"""Точка входа расширения: два инструмента для агента и два роута для ручной проверки.

Соглашения загрузчика Ouroboros, от которых здесь зависит работоспособность:

  * handler — второй позиционный аргумент register_tool, остальное keyword-only;
  * инструмент обязан вернуть строку. dict доедет до модели как Python-repr, а любое
    ложное значение (пустой dict, 0, None) схлопнется в пустую строку — поэтому
    json.dumps;
  * инструменты async: у синхронных in-process нет таймаута вообще, а корутину
    диспетчер оборачивает в asyncio.wait_for на отдельном потоке;
  * роуты, наоборот, СИНХРОННЫЕ. Асинхронный обработчик роута сервер выполняет прямо
    на своём event loop (gateway/extensions.py:605) и на время расчёта повесил бы весь
    HTTP-API; синхронный уходит в asyncio.to_thread. Возвращённый dict сервер сам
    заворачивает в JSON;
  * первый параметр инструмента не должен называться ctx/context/_ctx/tool_context —
    иначе загрузчик решит, что мы просим объект контекста, и вызовет handler(ctx, **args);
  * register() обязан оставаться дешёвым: torch и statsmodels импортируются внутри
    функций, а не здесь, иначе старт сервера ждал бы их загрузки.

Расчёт живёт в _forecast_payload/_shock_payload — синхронных и общих для обеих
поверхностей. Так роут по определению возвращает ровно то, что видит агент, ради чего
он и заведён.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any, Dict, List, Optional

from .lib import data, interpret, macro, scenario
from .lib.methods import HORIZONS, METHODS, Forecast, random_walk_drift

# Заполняются в register(): до него у скилла нет ни каталога состояния, ни данных
# о рантайме.
_SKILL_DIR: Optional[pathlib.Path] = None
_STATE_DIR: Optional[pathlib.Path] = None


def _dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _load(benchmark: str) -> data.Series:
    # Ключ читаем из окружения процесса, а не через api.get_settings: расширение
    # грузится внутри процесса сервера и видит переменные из docker-compose. Путь через
    # settings потребовал бы owner-grant, привязанного к хешу содержимого скилла,
    # и любая правка кода его сбрасывала бы.
    return data.load_weekly(
        benchmark=benchmark,
        skill_dir=_SKILL_DIR,
        state_dir=_STATE_DIR,
        api_key=os.environ.get("EIA_API_KEY", "").strip() or None,
    )


def _forecast_entry(forecast_result: Forecast) -> Dict[str, Any]:
    return {
        "method": forecast_result.method,
        "title": forecast_result.title,
        "median": round(forecast_result.median, 2),
        "low_q10": round(forecast_result.low, 2),
        "high_q90": round(forecast_result.high, 2),
    }


def _forecast_payload(
    benchmark: str = "brent",
    horizon: str = "3m",
    methods: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Прогноз двумя методами плюс бенчмарк. Ошибки возвращает полем error, не бросает."""
    try:
        if horizon not in HORIZONS:
            raise ValueError(
                f"горизонт '{horizon}' не поддерживается, доступны: {', '.join(HORIZONS)}"
            )
        requested = [m for m in (methods or list(METHODS)) if m in METHODS]
        if not requested:
            raise ValueError(
                f"не выбрано ни одного известного метода, доступны: {', '.join(METHODS)}"
            )

        series = _load(benchmark)
        steps = HORIZONS[horizon]

        results: List[Forecast] = []
        failures: Dict[str, str] = {}
        for name in requested:
            try:
                results.append(METHODS[name](series.values, steps))
            except Exception as exc:
                # Отказ одного метода не должен ронять весь ответ: второй метод и
                # бенчмарк остаются осмысленными, а факт отказа доедет до аналитика.
                failures[name] = f"{type(exc).__name__}: {exc}"

        benchmark_forecast = random_walk_drift(series.values, steps)
        meta = data.BENCHMARKS[series.benchmark]

        return {
            "benchmark": series.benchmark,
            "title": meta["title"],
            "horizon": horizon,
            "horizon_weeks": steps,
            "interval": "80 % (q10–q90)",
            "last_observation": {
                "date": series.last_date,
                "age_days": series.age_days,
                "value": round(series.last_value, 2),
                "units": "$/барр.",
                "source": series.source,
                "origin": series.origin,
            },
            "forecasts": [_forecast_entry(f) for f in results],
            "benchmark_model": _forecast_entry(benchmark_forecast),
            "failed_methods": failures,
            "interpretation": interpret.forecast_summary(
                title=meta["title"],
                horizon=horizon,
                last_date=series.last_date,
                last_value=series.last_value,
                age_days=series.age_days,
                origin_note=series.origin_note(),
                forecasts=results,
                benchmark=benchmark_forecast,
                failures=failures,
            ),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _shock_payload(
    cut_mbd: float,
    spot_price: Optional[float] = None,
    benchmark: str = "brent",
    world_supply_mbd: float = scenario.DEFAULT_WORLD_SUPPLY_MBD,
    offset_mbd: float = 0.0,
) -> Dict[str, Any]:
    """Диапазон цены при изменении добычи. Ошибки возвращает полем error, не бросает."""
    try:
        anchor_origin = "передана вызывающим"
        anchor_date = None
        anchor_age_days = None
        if spot_price is None:
            series = _load(benchmark)
            spot_price = series.last_value
            anchor_origin = series.origin_note()
            anchor_date = series.last_date
            anchor_age_days = series.age_days

        result = scenario.supply_shock(
            cut_mbd=float(cut_mbd),
            spot_price=float(spot_price),
            world_supply_mbd=float(world_supply_mbd),
            offset_mbd=float(offset_mbd),
        )
        meta = data.BENCHMARKS.get(benchmark, {"title": benchmark})
        result["anchor_origin"] = anchor_origin
        result["anchor_date"] = anchor_date
        result["anchor_age_days"] = anchor_age_days
        result["interpretation"] = interpret.shock_summary(result, meta["title"])
        return result
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _demand_shock_payload(
    gdp_change_pp: Optional[float] = None,
    demand_change_mbd: Optional[float] = None,
    spot_price: Optional[float] = None,
    benchmark: str = "brent",
    world_demand_mbd: float = scenario.DEFAULT_WORLD_DEMAND_MBD,
) -> Dict[str, Any]:
    """Диапазон цены при шоке спроса. Ошибки возвращает полем error, не бросает."""
    try:
        anchor_origin = "передана вызывающим"
        anchor_date = None
        anchor_age_days = None
        if spot_price is None:
            series = _load(benchmark)
            spot_price = series.last_value
            anchor_origin = series.origin_note()
            anchor_date = series.last_date
            anchor_age_days = series.age_days

        result = scenario.demand_shock(
            spot_price=float(spot_price),
            gdp_change_pp=None if gdp_change_pp is None else float(gdp_change_pp),
            demand_change_mbd=(
                None if demand_change_mbd is None else float(demand_change_mbd)
            ),
            world_demand_mbd=float(world_demand_mbd),
        )
        meta = data.BENCHMARKS.get(benchmark, {"title": benchmark})
        result["anchor_origin"] = anchor_origin
        result["anchor_date"] = anchor_date
        result["anchor_age_days"] = anchor_age_days
        result["interpretation"] = interpret.demand_shock_summary(result, meta["title"])
        return result
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _macro_payload() -> Dict[str, Any]:
    """Свежий макрофон. Ошибки возвращает полем error, не бросает."""
    try:
        result = macro.load_macro(_STATE_DIR)
        result["interpretation"] = interpret.macro_summary(
            result["blocks"], result["errors"]
        )
        return result
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# ─── Инструменты агента ────────────────────────────────────────────────────────
#
# async без единого await — намеренно, убирать нельзя. Диспетчер оборачивает корутину
# в asyncio.wait_for и тем даёт инструменту таймаут; у синхронного in-process
# обработчика таймаута нет вообще (tools/extension_dispatch.py:88-113), и зависший
# расчёт не прервался бы ничем.


async def forecast(
    benchmark: str = "brent",
    horizon: str = "3m",
    methods: Optional[List[str]] = None,
) -> str:
    """Прогноз цены на горизонте двумя независимыми методами плюс бенчмарк."""
    return _dumps(_forecast_payload(benchmark, horizon, methods))


async def supply_shock(
    cut_mbd: float,
    spot_price: Optional[float] = None,
    benchmark: str = "brent",
    world_supply_mbd: float = scenario.DEFAULT_WORLD_SUPPLY_MBD,
    offset_mbd: float = 0.0,
) -> str:
    """Диапазон цены при изменении добычи на cut_mbd млн барр./сутки."""
    return _dumps(
        _shock_payload(cut_mbd, spot_price, benchmark, world_supply_mbd, offset_mbd)
    )


async def demand_shock(
    gdp_change_pp: Optional[float] = None,
    demand_change_mbd: Optional[float] = None,
    spot_price: Optional[float] = None,
    benchmark: str = "brent",
    world_demand_mbd: float = scenario.DEFAULT_WORLD_DEMAND_MBD,
) -> str:
    """Диапазон цены при шоке спроса — прямом или через изменение мирового ВВП."""
    return _dumps(
        _demand_shock_payload(
            gdp_change_pp, demand_change_mbd, spot_price, benchmark, world_demand_mbd
        )
    )


async def macro_snapshot() -> str:
    """Свежий макрофон для нефти: доллар, ставки, глобальная активность, ЦБ РФ."""
    return _dumps(_macro_payload())


# ─── HTTP-роуты: только для ручной проверки ────────────────────────────────────
#
# Синхронные намеренно, см. шапку модуля. Параметры берём из query string, чтобы
# проверять curl-ом без тела запроса. Ошибку разбора возвращаем полем error, а не
# статусом: так поведение совпадает с инструментами, и роут остаётся зеркалом того,
# что видит агент.


def _query_float(params: Any, name: str, default: Optional[float]) -> Optional[float]:
    raw = params.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


def route_forecast(request: Any) -> Dict[str, Any]:
    params = request.query_params
    raw_methods = params.get("methods")
    return _forecast_payload(
        benchmark=params.get("benchmark") or "brent",
        horizon=params.get("horizon") or "3m",
        methods=[m.strip() for m in raw_methods.split(",") if m.strip()] if raw_methods else None,
    )


def route_supply_shock(request: Any) -> Dict[str, Any]:
    params = request.query_params
    try:
        cut_mbd = _query_float(params, "cut_mbd", None)
        if cut_mbd is None:
            return {"error": "обязательный параметр cut_mbd не задан"}
        return _shock_payload(
            cut_mbd=cut_mbd,
            spot_price=_query_float(params, "spot_price", None),
            benchmark=params.get("benchmark") or "brent",
            world_supply_mbd=_query_float(
                params, "world_supply_mbd", scenario.DEFAULT_WORLD_SUPPLY_MBD
            ),
            offset_mbd=_query_float(params, "offset_mbd", 0.0) or 0.0,
        )
    except ValueError as exc:
        return {"error": f"параметр не число: {exc}"}


def route_demand_shock(request: Any) -> Dict[str, Any]:
    params = request.query_params
    try:
        return _demand_shock_payload(
            gdp_change_pp=_query_float(params, "gdp_change_pp", None),
            demand_change_mbd=_query_float(params, "demand_change_mbd", None),
            spot_price=_query_float(params, "spot_price", None),
            benchmark=params.get("benchmark") or "brent",
            world_demand_mbd=_query_float(
                params, "world_demand_mbd", scenario.DEFAULT_WORLD_DEMAND_MBD
            ),
        )
    except ValueError as exc:
        return {"error": f"параметр не число: {exc}"}


def route_macro(request: Any) -> Dict[str, Any]:
    return _macro_payload()


def register(api) -> None:
    global _SKILL_DIR, _STATE_DIR

    info = api.get_runtime_info()
    _SKILL_DIR = pathlib.Path(info["skill_dir"])
    _STATE_DIR = pathlib.Path(info["state_dir"])

    api.register_tool(
        "forecast",
        forecast,
        description=(
            "Прогноз спотовой цены Brent или WTI на 1/3/6/12 месяцев по историческому "
            "ряду EIA. Считает двумя независимыми методами (Chronos-Bolt и "
            "экспоненциальное сглаживание) и добавляет бенчмарк случайного блуждания. "
            "Возвращает JSON: медиану и границы 80-процентного интервала (q10–q90) по "
            "каждому методу, дату, значение и ВОЗРАСТ В ДНЯХ последнего наблюдения "
            "(last_observation.age_days) с источником и краткую интерпретацию на "
            "русском. Использовать вместо самостоятельной прикидки, когда спрашивают "
            "о будущей цене. "
            "Якорь прогноза — последнее наблюдение, а не сегодняшняя цена: спот EIA "
            "выходит с лагом около недели. Возраст якоря приводить в ответе; если "
            "рынок с той даты сдвинулся, сказать об этом, но не подменять выход "
            "модели своей оценкой. "
            "Если методы разошлись, приводить обе медианы, а не усреднять их. "
            "Решений ОПЕК+, санкций и геополитики модель не знает — эти факторы "
            "накладывать поверх самостоятельно; свежий макрофон даёт macro_snapshot."
        ),
        schema={
            "type": "object",
            "properties": {
                "benchmark": {
                    "type": "string",
                    "enum": sorted(data.BENCHMARKS),
                    "description": "Эталонный сорт. По умолчанию brent.",
                },
                "horizon": {
                    "type": "string",
                    "enum": list(HORIZONS),
                    "description": "Горизонт прогноза. По умолчанию 3m.",
                },
                "methods": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(METHODS)},
                    "description": "Какие методы считать. По умолчанию оба.",
                },
            },
        },
        # С запасом на первый вызов: он включает загрузку весов Chronos с диска,
        # дальше пайплайн живёт в модульной глобали и вызовы идут быстро.
        timeout_sec=180,
    )

    api.register_tool(
        "supply_shock",
        supply_shock,
        description=(
            "Оценка диапазона цены при изменении добычи на N млн барр./сутки — "
            "сравнительная статика через краткосрочные ценовые эластичности спроса и "
            "предложения, а НЕ прогноз временного ряда. Возвращает JSON: центральную "
            "оценку, диапазон по жёсткости рынка, сами использованные эластичности с "
            "источником и интерпретацию с оговорками. Использовать для вопросов вида "
            "«что будет с ценой, если ОПЕК+ срежет добычу на 2 млн б/с»."
        ),
        schema={
            "type": "object",
            "properties": {
                "cut_mbd": {
                    "type": "number",
                    "description": (
                        "Сокращение добычи, млн барр./сутки. Отрицательное значение "
                        "означает наращивание."
                    ),
                },
                "spot_price": {
                    "type": "number",
                    "description": (
                        "Якорная цена, $/барр. По умолчанию берётся последнее "
                        "наблюдение выбранного эталона."
                    ),
                },
                "benchmark": {
                    "type": "string",
                    "enum": sorted(data.BENCHMARKS),
                    "description": "Откуда брать якорную цену. По умолчанию brent.",
                },
                "world_supply_mbd": {
                    "type": "number",
                    "description": (
                        "Мировое предложение жидких углеводородов, млн б/с. По "
                        "умолчанию 103. Передавать актуальное из свежего STEO или MOMR."
                    ),
                },
                "offset_mbd": {
                    "type": "number",
                    "description": (
                        "Сколько выпадения компенсируется свободными мощностями или "
                        "резервами, млн б/с. По умолчанию 0."
                    ),
                },
            },
            "required": ["cut_mbd"],
        },
        timeout_sec=60,
    )

    api.register_tool(
        "demand_shock",
        demand_shock,
        description=(
            "Оценка диапазона цены при шоке спроса — сравнительная статика через "
            "эластичности, а НЕ прогноз временного ряда. Шок задаётся ровно одним из "
            "двух способов: gdp_change_pp (изменение темпа роста мирового ВВП в "
            "процентных пунктах; переводится в спрос через эластичность по доходу 0,5 "
            "с источником) или demand_change_mbd (изменение спроса в млн барр./сутки "
            "напрямую). Возвращает JSON: центральную оценку, диапазон по жёсткости "
            "рынка, использованные эластичности с источниками и интерпретацию с "
            "оговорками. Использовать для вопросов вида «что будет с ценой, если рост "
            "мирового ВВП замедлится на 1 п.п.» или «если спрос упадёт на 1 млн б/с»."
        ),
        schema={
            "type": "object",
            "properties": {
                "gdp_change_pp": {
                    "type": "number",
                    "description": (
                        "Изменение темпа роста мирового ВВП, процентные пункты. "
                        "Отрицательное значение — замедление. Взаимоисключим с "
                        "demand_change_mbd."
                    ),
                },
                "demand_change_mbd": {
                    "type": "number",
                    "description": (
                        "Изменение мирового спроса, млн барр./сутки. Отрицательное "
                        "значение — падение спроса. Взаимоисключим с gdp_change_pp."
                    ),
                },
                "spot_price": {
                    "type": "number",
                    "description": (
                        "Якорная цена, $/барр. По умолчанию берётся последнее "
                        "наблюдение выбранного эталона."
                    ),
                },
                "benchmark": {
                    "type": "string",
                    "enum": sorted(data.BENCHMARKS),
                    "description": "Откуда брать якорную цену. По умолчанию brent.",
                },
                "world_demand_mbd": {
                    "type": "number",
                    "description": (
                        "Мировой спрос на жидкие углеводороды, млн б/с. По умолчанию "
                        "103. Передавать актуальное из свежего STEO или MOMR."
                    ),
                },
            },
        },
        timeout_sec=60,
    )

    api.register_tool(
        "macro_snapshot",
        macro_snapshot,
        description=(
            "Свежий макрофон для нефтяного рынка: индекс доллара, ставка ФРС, "
            "инфляция США и инфляционные ожидания, индекс глобальной активности "
            "Килиана (IGREA), ключевая ставка ЦБ РФ и курс USD/RUB. Возвращает JSON: "
            "по каждому показателю последнее значение с датой, изменение за месяц и "
            "год, источник, пометку о свежести и подсказку о знаке влияния на нефть. "
            "Это качественный контекст ПОВЕРХ модельного прогноза, а не вход модели: "
            "накладывая макрофон на прогноз, помечать вывод как суждение аналитика. "
            "Использовать вместе с forecast при вопросах о будущей цене и при вопросах "
            "о макрообстановке."
        ),
        schema={"type": "object", "properties": {}},
        timeout_sec=60,
    )

    api.register_route("forecast", route_forecast, methods=("GET",))
    api.register_route("supply_shock", route_supply_shock, methods=("GET",))
    api.register_route("demand_shock", route_demand_shock, methods=("GET",))
    api.register_route("macro", route_macro, methods=("GET",))
