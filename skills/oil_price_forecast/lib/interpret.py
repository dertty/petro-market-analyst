"""Краткая интерпретация результата по-русски.

Отдельный модуль, потому что интерпретация — не украшение вывода, а часть контракта:
голые числа аналитик подаст как факт, а это выход модели. Формулировки здесь заведомо
осторожные и обязаны называть слабость расчёта, когда она есть.
"""

from __future__ import annotations

from typing import Dict, List

from .methods import Forecast

HORIZON_LABELS = {"1m": "1 месяц", "3m": "3 месяца", "6m": "6 месяцев", "12m": "12 месяцев"}

# Насколько медиана метода должна отойти от случайного блуждания, чтобы считать, что
# метод вообще что-то сказал. Ниже этого порога прогноз — пересказ последней цены.
BENCHMARK_TOLERANCE_PCT = 3.0

# Расхождение медиан, после которого методы уже нельзя подавать как согласные.
DISAGREEMENT_PCT = 10.0

# Возраст последнего наблюдения, до которого лаг считается нормальным. Спотовые серии
# EIA публикуются с задержкой около недели — при исправном конвейере якорь и должен
# быть недельной давности. Что больше, наш собственный сбой: не обновился кэш либо
# ряд взят из снапшота.
NORMAL_ANCHOR_DAYS = 10


def _pct(part: float, whole: float) -> float:
    return abs(part) / abs(whole) * 100 if whole else float("inf")


def _anchor_age_note(age_days: int) -> str:
    """Что означает возраст якоря и что с ним делать.

    Возраст выводится всегда, а не только когда он велик: сам по себе недельный лаг
    нормален, но именно на нём построен весь уровень прогноза, и подать якорь как
    сегодняшнюю цену — самая частая ошибка при чтении этого ответа.
    """
    if age_days > NORMAL_ANCHOR_DAYS:
        return (
            f"ВНИМАНИЕ: якорю {age_days} дн., это больше обычного лага публикации EIA "
            f"(до {NORMAL_ANCHOR_DAYS} дн.) — вероятно, ряд не обновился или взят из "
            "снапшота. Уровень прогноза настолько же устарел; перепроверить текущую "
            "цену отдельно и сказать об этом в ответе."
        )
    return (
        "Якорь — последнее опубликованное наблюдение, а не сегодняшняя цена: спот EIA "
        "выходит с лагом около недели. Весь уровень прогноза отсчитывается от него, "
        "поэтому движение рынка после этой даты в расчёт не вошло. Если оно было, "
        "назвать его в ответе и сопоставить с прогнозом — но не подменять им выход "
        "модели."
    )


def forecast_summary(
    title: str,
    horizon: str,
    last_date: str,
    last_value: float,
    age_days: int,
    origin_note: str,
    forecasts: List[Forecast],
    benchmark: Forecast,
    failures: Dict[str, str],
) -> str:
    label = HORIZON_LABELS.get(horizon, horizon)
    lines = [
        f"{title}: последнее наблюдение {last_value:.2f} $/барр. за {last_date} — "
        f"это {age_days} дн. назад ({origin_note}). Горизонт — {label}."
    ]
    lines.append(_anchor_age_note(age_days))

    if not forecasts:
        lines.append("Ни один метод не отработал, содержательного прогноза нет.")
    else:
        medians = [f.median for f in forecasts]

        if len(forecasts) >= 2:
            gap = _pct(max(medians) - min(medians), sum(medians) / len(medians))
            if gap >= DISAGREEMENT_PCT:
                lines.append(
                    f"Методы расходятся: медианы отличаются на {gap:.0f} % "
                    f"({min(medians):.2f} против {max(medians):.2f} $/барр.). "
                    "Расхождение само по себе результат: единой оценки нет, "
                    "обе медианы равноправны."
                )
            else:
                lines.append(
                    f"Методы согласны: медианы отличаются всего на {gap:.0f} %, "
                    f"диапазон {min(medians):.2f}–{max(medians):.2f} $/барр."
                )

        near_benchmark = [
            f for f in forecasts
            if _pct(f.median - benchmark.median, benchmark.median) < BENCHMARK_TOLERANCE_PCT
        ]
        if len(near_benchmark) == len(forecasts):
            names = " и ".join(f"«{f.title}»" for f in near_benchmark)
            # Явно называем каждый метод, который не превзошёл бенчмарк, а не «все
            # медианы»: без этого читается так, будто у случайного блуждания есть
            # какое-то особое право на медиану, а не наоборот — оно самое примитивное
            # из трёх и специально взято как нулевая гипотеза.
            lines.append(
                f"{names} не разошлись с наивным бенчмарком (случайное блуждание со "
                f"сносом) больше чем на {BENCHMARK_TOLERANCE_PCT:.0f} %. Значит ни один "
                "из них не нашёл сигнала о направлении сильнее, чем даёт простое "
                "продление тренда, — сама медиана здесь малоинформативна, содержателен "
                "только размах коридора."
            )

        widest = max(forecasts, key=lambda f: f.high - f.low)
        width = _pct(widest.high - widest.low, last_value)
        lines.append(
            f"Самый широкий коридор — у метода «{widest.title}»: "
            f"{widest.low:.2f}–{widest.high:.2f} $/барр., это {width:.0f} % текущей цены. "
            "Ширина и есть главный результат: она показывает, какой разброс исходов "
            "рынок допускает на этом горизонте."
        )

    if failures:
        broken = ", ".join(f"{name} ({reason})" for name, reason in failures.items())
        lines.append(f"Не отработали: {broken}.")

    lines.append(
        "Оговорки: это выход статистической модели по историческому ряду спотовых цен, "
        "а не факт, не консенсус рынка и не фьючерсная кривая. Модели не знают ни о "
        "решениях ОПЕК+, ни о санкциях, ни о геополитике — этих факторов в расчёте нет. "
        "Интервал 80 % (q10–q90) означает, что вне его рынок оказывается примерно в "
        "одном случае из пяти."
    )
    return " ".join(lines)


def _anchor_stamp(result: Dict[str, object]) -> str:
    """«(наблюдение за 2026-08-03, 7 дн. назад)» для якорной цены сценарных моделей.

    Пусто, когда цену передал вызывающий: тогда её свежесть — на его совести, и
    приписывать ей дату ряда было бы неверно.
    """
    anchor_date = result.get("anchor_date")
    age_days = result.get("anchor_age_days")
    if not anchor_date or age_days is None:
        return ""
    return f" (наблюдение за {anchor_date}, {age_days} дн. назад)"


def shock_summary(result: Dict[str, object], benchmark_title: str) -> str:
    net_cut = float(result["net_cut_mbd"])
    low, high = result["price_range"]  # type: ignore[misc]
    direction = "сокращение" if net_cut > 0 else "наращивание"

    return (
        f"{benchmark_title}: {direction} добычи на {abs(net_cut):.2f} млн барр./сутки — "
        f"это {abs(float(result['supply_change_pct'])):.2f} % мирового предложения "
        f"({result['world_supply_mbd']} млн б/с). От якорной цены "
        f"{result['anchor_price']} $/барр.{_anchor_stamp(result)} модель даёт центральную оценку "
        f"{result['central_price']} $/барр. ({float(result['central_change_pct']):+.1f} %) "
        f"и диапазон {low}–{high} $/барр. в зависимости от жёсткости рынка. "
        f"Использованы краткосрочные ценовые эластичности спроса 0,02–0,10 и предложения "
        f"0,02–0,12 по оценкам: {result['elasticity_source']}. "
        "Оговорки: это сравнительная статика — модель говорит, где установится цена, но не "
        "говорит, за сколько недель. Запасы и стратегические резервы гасят шок и в формуле "
        "не учтены, поэтому на коротком горизонте фактическая реакция обычно слабее. "
        "Эластичности взяты из литературы, а не оценены на этих данных, и при больших "
        "выпадениях линейное приближение завышает эффект."
    )


def demand_shock_summary(result: Dict[str, object], benchmark_title: str) -> str:
    demand_change = float(result["demand_change_mbd"])
    low, high = result["price_range"]  # type: ignore[misc]
    direction = "рост" if demand_change > 0 else "падение"

    if result.get("gdp_change_pp") is not None:
        gdp = float(result["gdp_change_pp"])  # type: ignore[arg-type]
        gdp_direction = "ускорение" if gdp > 0 else "замедление"
        low_e, high_e = result["income_elasticity_range"]  # type: ignore[misc]
        origin = (
            f"{gdp_direction} роста мирового ВВП на {abs(gdp):.1f} п.п. при эластичности "
            f"спроса по доходу {result['income_elasticity']} "
            f"({result['income_elasticity_source']}) — это {direction} спроса на "
            f"{abs(demand_change):.2f} млн барр./сутки"
        )
        elasticity_caveat = (
            f"Сама эластичность по доходу неопределённа: в литературе оценки "
            f"{low_e}–{high_e}, взято центральное значение. "
        )
    else:
        origin = f"{direction} спроса на {abs(demand_change):.2f} млн барр./сутки"
        elasticity_caveat = ""

    return (
        f"{benchmark_title}: {origin}, или "
        f"{abs(float(result['demand_change_pct'])):.2f} % мирового спроса "
        f"({result['world_demand_mbd']} млн б/с). От якорной цены "
        f"{result['anchor_price']} $/барр.{_anchor_stamp(result)} модель даёт центральную оценку "
        f"{result['central_price']} $/барр. ({float(result['central_change_pct']):+.1f} %) "
        f"и диапазон {low}–{high} $/барр. в зависимости от жёсткости рынка. "
        f"Использованы краткосрочные ценовые эластичности спроса 0,02–0,10 и предложения "
        f"0,02–0,12 по оценкам: {result['elasticity_source']}. "
        f"Оговорки: {elasticity_caveat}Это сравнительная статика — модель говорит, где "
        "установится цена, но не говорит, за сколько недель. Ценовые эластичности взяты "
        "из литературы, а не оценены на этих данных, и при больших шоках линейное "
        "приближение завышает эффект."
    )


def macro_summary(blocks: Dict[str, List[Dict]], errors: Dict[str, str]) -> str:
    """Сводка макрофона: по фразе на блок плюс обязательная оговорка о статусе чисел."""
    from .macro import BLOCK_ORDER, BLOCK_TITLES  # локально: избегаем цикла импортов

    lines: List[str] = []
    for block in BLOCK_ORDER:
        entries = blocks.get(block) or []
        if not entries:
            continue
        parts = []
        for entry in entries:
            latest = entry["latest"]
            change = entry.get("change_1m")
            fragment = f"{entry['title']}: {latest['value']} ({latest['date']}"
            if change is not None:
                if entry.get("id") == "usd_rub":
                    # Направление прописью: рост курса USD/RUB — это ослабление рубля,
                    # и на голом «+7.5 %» модель уже переворачивала знак в ответе.
                    verb = "рубль ослаб" if change > 0 else "рубль укрепился"
                    fragment += f", за месяц {verb} на {abs(change):g} %"
                else:
                    fragment += f", за месяц {change:+g} {entry['change_units']}"
            fragment += ")"
            if entry.get("origin") == "stale_cache":
                fragment += " [УСТАРЕВШИЙ КЭШ]"
            parts.append(fragment)
        lines.append(f"{BLOCK_TITLES[block]} — " + "; ".join(parts) + ".")

    if errors:
        broken = ", ".join(sorted(errors))
        lines.append(f"Не удалось обновить: {broken} (подробности в поле errors).")

    lines.append(
        "Оговорки: макропоказатели — качественный контекст поверх модельного прогноза. "
        "Модель прогноза одномерна и этих рядов не видит; знаки влияния из oil_note — "
        "ориентировочные статистические связи, а не механика."
    )
    return " ".join(lines)
