"""Шоки предложения и спроса: во сколько обходится рынку сдвиг N млн барр./сутки.

Это не прогноз временного ряда, и путать их нельзя. Вопрос «что будет с ценой через
три месяца» и вопрос «что будет с ценой, если ОПЕК+ срежет 2 млн б/с» — разной природы:
второй спрашивает про сравнительную статику, где ответ определяется не историей ряда,
а тем, насколько круто спрос и предложение реагируют на цену.

Модель — учебная, и здесь это достоинство: каждое допущение видно и проверяемо.

    ΔP/P ≈ −(ΔQ/Q) / (|η_d| + η_s)

Ограничения, которые обязаны доехать до аналитика вместе с числом:
  * это сравнительная статика без динамики — модель не говорит, за сколько недель
    цена придёт в новую точку;
  * запасы и стратегические резервы гасят шок, и в формуле их нет;
  * при больших ΔQ линейное приближение завышает эффект: реальные эластичности
    растут по мере роста цены;
  * эластичности взяты из литературы, а не оценены на наших данных.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

# Краткосрочные ценовые эластичности мирового рынка нефти. Диапазоны, а не точки:
# в литературе разброс оценок больше, чем неопределённость внутри любой отдельной
# работы, и делать вид, что мы знаем число до второго знака, нечестно.
#
# Источники: Hamilton (2009) «Understanding Crude Oil Prices»; Baumeister & Peersman
# (2013) «The Role of Time-Varying Price Elasticities»; Caldara, Cavallo & Iacoviello
# (2019) «Oil Price Elasticities and Oil Price Fluctuations».
ELASTICITY_SOURCE = (
    "Hamilton (2009); Baumeister & Peersman (2013); Caldara, Cavallo & Iacoviello (2019)"
)
DEMAND_ELASTICITY = {"low": 0.02, "central": 0.06, "high": 0.10}   # |η_d|
SUPPLY_ELASTICITY = {"low": 0.02, "central": 0.07, "high": 0.12}   # η_s

# Мировое предложение жидких углеводородов, млн барр./сутки. Значение по умолчанию,
# агент может передать актуальное из свежего STEO или MOMR.
DEFAULT_WORLD_SUPPLY_MBD = 103.0

# Эластичность спроса на нефть по доходу: на сколько процентов меняется спрос при
# изменении мирового ВВП на процент. Центральное значение 0,5 даёт удобное для проверки
# правило «1 п.п. мирового роста ≈ 0,5 млн б/с спроса» при рынке ~103 млн б/с.
# Диапазон в литературе широк (ОЭСР ниже, вне ОЭСР выше), поэтому границы едут в
# оговорки, а не в отдельные ветки: ветки жёсткости рынка уже дают диапазон цены.
#
# Источники: Gately & Huntington (2002) «The Asymmetric Effects of Changes in Price
# and Income on Energy and Oil Demand» — долгосрочная ~0,55 ОЭСР / ~1,0 вне ОЭСР;
# IMF World Economic Outlook, апрель 2011, гл. 3 — краткосрочная ~0,68;
# Hamilton (2009) «Understanding Crude Oil Prices».
INCOME_ELASTICITY_OF_DEMAND = 0.5
INCOME_ELASTICITY_RANGE = (0.3, 0.8)
INCOME_ELASTICITY_SOURCE = (
    "Gately & Huntington (2002); IMF WEO апрель 2011, гл. 3; Hamilton (2009)"
)

# Мировой спрос на уровне точности этой модели равен предложению: разница между ними —
# изменение запасов, которое сравнительная статика всё равно не учитывает.
DEFAULT_WORLD_DEMAND_MBD = DEFAULT_WORLD_SUPPLY_MBD


class ScenarioError(ValueError):
    """Параметры сценария бессмысленны — считать нечего."""


@dataclass
class ShockBranch:
    """Одна ветка сценария: жёсткость рынка задаёт величину реакции цены."""

    key: str
    label: str
    demand_elasticity: float
    supply_elasticity: float
    price_change_pct: float
    price: float


def supply_shock(
    cut_mbd: float,
    spot_price: float,
    world_supply_mbd: float = DEFAULT_WORLD_SUPPLY_MBD,
    offset_mbd: float = 0.0,
) -> Dict[str, object]:
    """Диапазон цены при выпадении cut_mbd млн б/с.

    offset_mbd — то, что рынок компенсирует свободными мощностями или разбронированием
    резервов. Вычитается из шока, потому что до цены доходит только нетто-выпадение.
    Отрицательный cut_mbd означает наращивание добычи и даёт снижение цены.
    """
    if spot_price <= 0:
        raise ScenarioError("якорная цена должна быть положительной")
    if world_supply_mbd <= 0:
        raise ScenarioError("мировое предложение должно быть положительным")

    net_cut = float(cut_mbd) - float(offset_mbd)
    if abs(net_cut) >= world_supply_mbd:
        raise ScenarioError(
            "нетто-шок сопоставим со всем мировым предложением — линейная модель "
            "эластичностей на таких величинах бессмысленна"
        )

    supply_change = net_cut / float(world_supply_mbd)

    branches: List[ShockBranch] = []
    # Чем ниже эластичности, тем жёстче рынок и тем сильнее реакция цены на то же
    # выпадение. При сокращении добычи ветка «жёсткий рынок» даёт верхнюю границу
    # диапазона, при наращивании — нижнюю, поэтому границы берутся через min/max,
    # а не по порядку веток.
    for key, label, elasticity_key in (
        ("flexible", "Гибкий рынок (высокие эластичности)", "high"),
        ("central", "Центральный сценарий", "central"),
        ("tight", "Жёсткий рынок (низкие эластичности)", "low"),
    ):
        eta_d = DEMAND_ELASTICITY[elasticity_key]
        eta_s = SUPPLY_ELASTICITY[elasticity_key]
        change = supply_change / (eta_d + eta_s)
        branches.append(
            ShockBranch(
                key=key,
                label=label,
                demand_elasticity=eta_d,
                supply_elasticity=eta_s,
                price_change_pct=round(change * 100, 1),
                price=round(spot_price * (1 + change), 2),
            )
        )

    prices = [branch.price for branch in branches]
    central = next(b for b in branches if b.key == "central")
    return {
        "net_cut_mbd": round(net_cut, 3),
        "supply_change_pct": round(supply_change * 100, 2),
        "anchor_price": round(float(spot_price), 2),
        "world_supply_mbd": float(world_supply_mbd),
        "branches": [branch.__dict__ for branch in branches],
        "price_range": [min(prices), max(prices)],
        "central_price": central.price,
        "central_change_pct": central.price_change_pct,
        "elasticity_source": ELASTICITY_SOURCE,
        "model": "ΔP/P ≈ −(ΔQ/Q) / (|η_d| + η_s), сравнительная статика",
    }


def demand_shock(
    spot_price: float,
    gdp_change_pp: Optional[float] = None,
    demand_change_mbd: Optional[float] = None,
    world_demand_mbd: float = DEFAULT_WORLD_DEMAND_MBD,
    income_elasticity: float = INCOME_ELASTICITY_OF_DEMAND,
) -> Dict[str, object]:
    """Диапазон цены при шоке спроса — заданном напрямую или через изменение ВВП.

    Задать нужно ровно один из gdp_change_pp (изменение темпа роста мирового ВВП,
    процентные пункты) и demand_change_mbd (изменение спроса, млн б/с): оба сразу —
    противоречие, ни одного — считать нечего. Отрицательные значения означают
    замедление ВВП или падение спроса и дают снижение цены.
    """
    if spot_price <= 0:
        raise ScenarioError("якорная цена должна быть положительной")
    if world_demand_mbd <= 0:
        raise ScenarioError("мировой спрос должен быть положительным")
    if (gdp_change_pp is None) == (demand_change_mbd is None):
        raise ScenarioError(
            "нужно задать ровно один из gdp_change_pp и demand_change_mbd"
        )

    gdp_mapping_used = demand_change_mbd is None
    if gdp_mapping_used:
        demand_change_mbd = (
            float(gdp_change_pp) / 100.0 * float(income_elasticity) * float(world_demand_mbd)
        )
    demand_change_mbd = float(demand_change_mbd)
    if abs(demand_change_mbd) >= world_demand_mbd:
        raise ScenarioError(
            "шок спроса сопоставим со всем мировым спросом — линейная модель "
            "эластичностей на таких величинах бессмысленна"
        )

    demand_change = demand_change_mbd / float(world_demand_mbd)

    branches: List[ShockBranch] = []
    # Знак противоположен шоку предложения: рост спроса толкает цену вверх, поэтому
    # ΔP/P = +(ΔQd/Q)/(|η_d|+η_s). Логика веток та же — чем ниже эластичности, тем
    # жёстче рынок и тем сильнее реакция цены; границы диапазона через min/max.
    for key, label, elasticity_key in (
        ("flexible", "Гибкий рынок (высокие эластичности)", "high"),
        ("central", "Центральный сценарий", "central"),
        ("tight", "Жёсткий рынок (низкие эластичности)", "low"),
    ):
        eta_d = DEMAND_ELASTICITY[elasticity_key]
        eta_s = SUPPLY_ELASTICITY[elasticity_key]
        change = demand_change / (eta_d + eta_s)
        branches.append(
            ShockBranch(
                key=key,
                label=label,
                demand_elasticity=eta_d,
                supply_elasticity=eta_s,
                price_change_pct=round(change * 100, 1),
                price=round(spot_price * (1 + change), 2),
            )
        )

    prices = [branch.price for branch in branches]
    central = next(b for b in branches if b.key == "central")
    result: Dict[str, object] = {
        "demand_change_mbd": round(demand_change_mbd, 3),
        "demand_change_pct": round(demand_change * 100, 2),
        "gdp_change_pp": None if gdp_change_pp is None else float(gdp_change_pp),
        "anchor_price": round(float(spot_price), 2),
        "world_demand_mbd": float(world_demand_mbd),
        "branches": [branch.__dict__ for branch in branches],
        "price_range": [min(prices), max(prices)],
        "central_price": central.price,
        "central_change_pct": central.price_change_pct,
        "elasticity_source": ELASTICITY_SOURCE,
        "model": "ΔP/P ≈ +(ΔQd/Q) / (|η_d| + η_s), сравнительная статика",
    }
    if gdp_mapping_used:
        # Эластичность по доходу возвращается только когда она реально участвовала:
        # при прямом задании спроса цитировать её было бы не за что.
        result["income_elasticity"] = float(income_elasticity)
        result["income_elasticity_range"] = list(INCOME_ELASTICITY_RANGE)
        result["income_elasticity_source"] = INCOME_ELASTICITY_SOURCE
    return result
