"""Readable, data-only local explanations; API prose remains optional."""
from __future__ import annotations

ROLE_RU = {"consolidator": "сбор средств", "transit": "передача средств дальше",
           "distributor": "распределение средств", "terminal": "возможное завершение потока",
           "coordinator": "связи между группами", "peripheral": "слабые ролевые признаки"}


def amount(value: float) -> str:
    # Compact, explicitly approximate for abbreviated numbers; never scientific.
    value = float(value)
    if abs(value) >= 1_000_000:
        return f"≈{value / 1_000_000:.2f} млн ₸".replace(".", ",")
    if abs(value) >= 1_000:
        return f"≈{format(value / 1_000, '.1f').replace('.', ',')} тыс. ₸"
    return f"{value:.2f} ₸".replace(".", ",")


def caution(row) -> str:
    if int(row.get("depth", 0)) == 4:
        return " Глубина 4: исходящие потоки могут быть скрыты."
    if bool(row.get("is_seed", False)):
        return " Внешние поступления могут быть скрыты."
    return " Выборка неполная."


def role_evidence(row) -> str:
    role = str(row["role"])
    ni, no = int(row["in_deg"]), int(row["out_deg"])
    if role == "transit":
        timed = float(row["fifo_1d"]) >= .8
        reason = "Есть совпадение сумм в тот же/следующий день" if timed else "Объёмы близки; быстрое прохождение не подтверждено"
        text = f"Гипотеза транзита: {ni} отправ., {no} получ. {reason}."
    elif role == "consolidator":
        text = f"Гипотеза сбора: {ni} отправителей; вход {amount(row['in_kzt'])}, выход {amount(row['out_kzt'])}. Входящий объём преобладает."
    elif role == "distributor":
        text = f"Гипотеза распределения: {no} получателей; выход {amount(row['out_kzt'])}. Исходящий объём преобладает."
    elif role == "terminal":
        text = f"Возможный конечный получатель: вход {amount(row['in_kzt'])}, наблюдаемых выходов 0. Это не полный баланс."
    elif role == "coordinator":
        text = f"Гипотеза связующего узла: {ni} отправ., {no} получ.; связи с {int(row.get('clusters_touched', 0))} группами. Проверить маршруты."
    else:
        text = f"Недостаточно признаков специальной роли: {ni} отправителей, {no} получателей. Это не отсутствие риска."
    result = text + caution(row)
    if len(result) > 200:
        result = f"Гипотеза: {ROLE_RU[role]}. Отправителей {ni}, получателей {no}." + caution(row)
    return result


def priority_why(row) -> str:
    reason = {"consolidator": "сбор средств", "transit": "передачу средств дальше",
              "distributor": "распределение средств", "terminal": "возможное завершение потока",
              "coordinator": "связи между группами", "peripheral": "полноту данных"}[str(row["role"])]
    weights = {"connectivity": .30, "exposure": .22, "seed": .18, "temporal": .15, "role": .10, "uncertainty": .05}
    labels = {"connectivity": "связность", "exposure": "масштаб и влияние", "seed": "связи с исходными узлами",
              "temporal": "временные связи", "role": "ролевые признаки", "uncertainty": "неопределённость роли"}
    main = max(weights, key=lambda key: weights[key] * float(row.get("priority_" + key, 0)))
    text = (f"Отправителей {int(row['in_deg'])}; вход {amount(row['in_kzt'])}, "
            f"выход {amount(row['out_kzt'])}. Приоритет: {labels[main]}. Проверить {reason}.")
    result = text + caution(row)
    if len(result) > 200:
        result = f"Отправителей {int(row['in_deg'])}, получателей {int(row['out_deg'])}. Проверить {reason}." + caution(row)
    return result
