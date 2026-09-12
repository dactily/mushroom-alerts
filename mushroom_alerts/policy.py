"""Central, versioned rule parameters used by calculations and documentation."""

RULES_VERSION = "4"

API30_DECAY = 0.93
API30_WINDOW_DAYS = 30
API30_LAG_OFFSET = 0
API30_MAX_GAPS = 3
API30_THRESHOLD_MM = 25.0
API30_BANDS_MM = (15.0, 25.0, 40.0)
API30_T_MEAN_MIN = 8.0
API30_T_MEAN_MAX = 22.0
API30_T_MIN_ABOVE = 2.0

RAIN_EPISODE_DAYS = 3
RAIN_EPISODE_MM = 20.0
RAIN_T_MEAN_MIN = 12.0
RAIN_T_MEAN_MAX = 22.0
GROWTH_WINDOW_FROM_DAYS = 7
GROWTH_WINDOW_TO_DAYS = 12
GROWTH_RESIDUAL_TO_DAYS = 21
NEXT_RAIN_MM = 5.0

BIOLOGICAL_MAP_LEVEL = 4.0
BIOLOGICAL_VERDICT_LABELS = {
    "insufficient": "недостаточно данных",
    "low": "низкая",
    "medium": "средняя",
    "high": "высокая",
}

# ----------------------------------------------------------------------
# Chance in percent (PLAN §9b).  One comparable number per location, so a
# list of twenty forests carries information the four-word verdict cannot.
# Not calibrated: it says "how much this place today looks like the
# conditions under which mushrooms come", not a measured find frequency.
# ----------------------------------------------------------------------

#: Base by rain-episode phase; every other factor multiplies this.
CHANCE_PHASE_BASE = {
    "primary_window": 0.60,
    "residual_window": 0.35,
    "waiting": 0.15,
    "expired": 0.05,
    "no_episode": 0.05,
}

#: Multiplier per :data:`API30_BANDS_MM` band, driest first (one more
#: factor than there are edges).
CHANCE_MOISTURE_FACTORS = (0.5, 0.8, 1.15, 1.25)

#: No usable API30 number: no moisture evidence either way.
CHANCE_MOISTURE_UNKNOWN = 1.0

#: The API30 temperature gate is not satisfied (or is unknown).
CHANCE_TEMPERATURE_FAILED = 0.6

#: Frost within the completed station week.
CHANCE_FROST = 0.4

#: HoubyMapa score ``s`` (0..1) -> ``base + span * s``.
CHANCE_HOUBYMAPA_BASE = 0.85
CHANCE_HOUBYMAPA_SPAN = 0.3

#: ČHMÚ level -> ``base + step * (level - pivot)``.
CHANCE_CHMI_BASE = 0.9
CHANCE_CHMI_STEP = 0.05
CHANCE_CHMI_PIVOT = 3.0

#: Station missing or stale: the number stays a guess, so it is capped.
CHANCE_NO_STATION_CAP = 50

#: Reported in steps of 5 % between these bounds -- the model is not
#: accurate enough to pretend otherwise.
CHANCE_STEP = 5
CHANCE_MIN = 5
CHANCE_MAX = 95

#: Send rules over the chance (PLAN §9d).
CHANCE_MOVE_PCT = 10
CHANCE_ALERT_PCT = 60


def interpretation_guide() -> str:
    """Human guidance generated from the same constants as the rules."""
    dry, moderate, wet = API30_BANDS_MM
    return f"""Как читать (справка, стабильный текст):
1. API30 — сумма осадков за {API30_WINDOW_DAYS} дней с затуханием {API30_DECAY}/сут (формула ČHMÚ, воспроизведена точно).
2. Ориентировочные полосы API30, НЕ откалиброваны: <{dry:g} мм сухо, {dry:g}–{moderate:g} мм умеренно, {moderate:g}–{wet:g} мм хорошо, >{wet:g} мм очень влажно.
3. Рабочий порог API30 {API30_THRESHOLD_MM:g} мм пока не перекалиброван.
4. Дождевой эпизод: ≥{RAIN_EPISODE_MM:g} мм за {RAIN_EPISODE_DAYS} календарных дня при средней температуре {RAIN_T_MEAN_MIN:g}–{RAIN_T_MEAN_MAX:g} °C; предполагаемое окно D+{GROWTH_WINDOW_FROM_DAYS}...D+{GROWTH_WINDOW_TO_DAYS}.
5. Высокая вероятность допустима только внутри основного окна D+{GROWTH_WINDOW_FROM_DAYS}...D+{GROWTH_WINDOW_TO_DAYS}: до него свежий дождь даёт максимум среднюю. После него до D+{GROWTH_RESIDUAL_TO_DAYS} сохраняется остаточная вероятность максимум средней силы. Дополнительно для высокой нужны свежий API30 ≥{API30_THRESHOLD_MM:g} мм, средняя температура {API30_T_MEAN_MIN:g}–{API30_T_MEAN_MAX:g} °C, минимум > {API30_T_MIN_ABOVE:g} °C, достаточная история, отсутствие заморозка за 7 завершённых станционных суток и — только для сегодняшнего дня — свежая высокая поддержка хотя бы одной карты. Карты прогноза не публикуют, поэтому на будущие дни их уровень не влияет ни в плюс, ни в минус.
6. Шанс в процентах — сравнимое между локациями число, НЕ вероятность находки: база по фазе эпизода (основное окно {CHANCE_PHASE_BASE["primary_window"]:g}, остаточное {CHANCE_PHASE_BASE["residual_window"]:g}, ожидание {CHANCE_PHASE_BASE["waiting"]:g}, нет окна {CHANCE_PHASE_BASE["no_episode"]:g}) умножается на влагу по полосам API30, на {CHANCE_TEMPERATURE_FAILED:g} при невыполненном температурном условии, на {CHANCE_FROST:g} при заморозке и — только на сегодня — на карты. Округление до {CHANCE_STEP} %, диапазон {CHANCE_MIN}–{CHANCE_MAX} %; без свежей станции число ограничено {CHANCE_NO_STATION_CAP} %.
7. Карта ČHMÚ — модельный ориентир по микоризным видам (hřib, kozák, liška), а не доказательство наличия грибов; опята и дереворазрушающие виды она не описывает.
8. Прогноз Open-Meteo в этих точках исторически был примерно в 1.9 раза «мокрее» станции; дальше 7 дней — ненадёжно, говорить «ориентировочно».
9. HoubyMapa — независимая модель (радар ČHMÚ + Open-Meteo, влажность и температура почвы); совпадение с ČHMÚ усиливает вывод, расхождение требует осторожности.
10. Полные станционные сутки главнее модели; неполные сутки — нижняя оценка и не складываются с модельным днём.
11. Данные: zdroj ČHMÚ (CC BY 4.0), HoubyMapa.cz, Open-Meteo (CC BY 4.0)."""
