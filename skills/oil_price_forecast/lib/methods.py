"""Два метода прогноза плюс опорный бенчмарк, все на общей недельной сетке.

Интервал везде 80 % (q10–q90), а не привычные 95 %. Причина в Chronos-Bolt: обученные
квантили модели заканчиваются на 0.1 и 0.9, всё за их пределами пришлось бы
экстраполировать. Считать один метод на 80 %, а другой на 95 % — значит подсунуть
аналитику несопоставимые коридоры, поэтому уровень един для всех.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

# Горизонт в неделях. Верхняя точка — 52, и это не случайность: Chronos-Bolt считает
# до 64 шагов за один проход, а дальше пошла бы авторегрессионная раскрутка с
# накоплением ошибки.
HORIZONS: Dict[str, int] = {"1m": 4, "3m": 13, "6m": 26, "12m": 52}

# Сколько недель истории отдаём моделям. 512 недель ≈ 10 лет: хватает, чтобы застать
# и обвал 2014–2016, и 2020 год, то есть режимы с совсем разной волатильностью.
CONTEXT_WEEKS = 512

Q_LOW, Q_HIGH = 0.1, 0.9

# Квантиль 0.9 стандартного нормального распределения. Берём константой, чтобы не
# тащить scipy ради одного числа.
Z_90 = 1.2815515655446004

CHRONOS_MODEL = "amazon/chronos-bolt-small"

# Пайплайн живёт здесь между вызовами. Это работает ровно потому, что скилл не
# объявляет dependencies и грузится внутри процесса сервера: при изолированном
# .ouroboros_env расширение ушло бы в out-of-process-режим, где register() и весь
# модуль выполняются заново на каждый вызов, и веса читались бы с диска каждый раз.
_PIPELINE = None


@dataclass
class Forecast:
    """Точка на конце горизонта: медиана и границы 80-процентного коридора."""

    method: str
    title: str
    median: float
    low: float
    high: float


def _context(values: List[float]) -> List[float]:
    return [float(v) for v in values[-CONTEXT_WEEKS:]]


def chronos_forecast(values: List[float], steps: int) -> Forecast:
    """Chronos-Bolt, zero-shot. Квантили модель отдаёт сама, без нормальных допущений."""
    global _PIPELINE
    import torch
    from chronos import BaseChronosPipeline

    if _PIPELINE is None:
        # Веса лежат в образе (HF_HOME=/opt/huggingface), в сеть поход не нужен.
        _PIPELINE = BaseChronosPipeline.from_pretrained(CHRONOS_MODEL, device_map="cpu")

    context = torch.tensor(_context(values), dtype=torch.float32)
    quantiles, _mean = _PIPELINE.predict_quantiles(
        context,
        prediction_length=steps,
        quantile_levels=[Q_LOW, 0.5, Q_HIGH],
    )
    # (batch=1, steps, 3) → берём последний шаг горизонта.
    low, median, high = (float(x) for x in quantiles[0, steps - 1])
    return Forecast("chronos", f"Chronos-Bolt ({CHRONOS_MODEL})", median, low, high)


def ets_forecast(values: List[float], steps: int) -> Forecast:
    """Экспоненциальное сглаживание Хольта с демпфированным трендом по лог-ценам.

    Лог-шкала здесь не косметика: в уровнях цены модель разрешила бы отрицательный
    прогноз и дала бы симметричный коридор, тогда как у цены риск вверх и вниз
    несимметричен. Демпфирование тренда не даёт экстраполировать последний наклон
    на весь горизонт линейно.
    """
    import numpy as np
    import pandas as pd
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel

    # Именно pd.Series, а не ndarray: ETSResults.get_prediction обращается к .index
    # результата (statsmodels 0.14.6, ets.py:2267) и на массиве падает с AttributeError.
    y = pd.Series(np.log(np.asarray(_context(values), dtype=float)))
    fit = ETSModel(y, error="add", trend="add", damped_trend=True, seasonal=None).fit(
        disp=False
    )
    frame = fit.get_prediction(start=len(y), end=len(y) + steps - 1).summary_frame(
        alpha=1 - (Q_HIGH - Q_LOW)
    )
    row = frame.iloc[-1]
    # exp(среднего логарифма) — это медиана в уровнях цены, а не среднее; именно
    # медиану мы и заявляем в ответе.
    return Forecast(
        "ets",
        "Экспоненциальное сглаживание (Хольт, демпфированный тренд)",
        float(np.exp(row["mean"])),
        float(np.exp(row["pi_lower"])),
        float(np.exp(row["pi_upper"])),
    )


def random_walk_drift(values: List[float], steps: int) -> Forecast:
    """Случайное блуждание со сносом — эталон, который на ценах нефти трудно побить.

    Держим его в ответе не для полноты: если обе модели совпали с блужданием, то
    содержательного прогноза нет ни у одной, и честнее сказать это прямо.
    """
    import numpy as np

    y = np.log(np.asarray(_context(values), dtype=float))
    diffs = np.diff(y)
    drift = float(diffs.mean())
    sigma = float(diffs.std(ddof=1))

    center = y[-1] + drift * steps
    spread = Z_90 * sigma * (steps ** 0.5)
    return Forecast(
        "rw_drift",
        "Случайное блуждание со сносом (бенчмарк)",
        float(np.exp(center)),
        float(np.exp(center - spread)),
        float(np.exp(center + spread)),
    )


METHODS = {"chronos": chronos_forecast, "ets": ets_forecast}
