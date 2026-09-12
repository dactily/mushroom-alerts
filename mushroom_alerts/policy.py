"""Central, versioned rule parameters used by calculations and documentation."""

RULES_VERSION = "5"

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

#: Base by rain-episode timing: days since the episode anchor -> factor,
#: linear between the knots and clamped outside them.  A stand does not
#: switch on the morning D+7 and off the evening D+12; it ramps up over
#: days, holds, and fades.  The plateau covers the same primary window as
#: :data:`GROWTH_WINDOW_FROM_DAYS`..:data:`GROWTH_WINDOW_TO_DAYS`, and the
#: tail reaches its floor a few days past :data:`GROWTH_RESIDUAL_TO_DAYS`,
#: where the verdict already calls the window expired.
CHANCE_PHASE_RAMP = (
    (0, 0.05),
    (5, 0.15),
    (7, 0.60),
    (12, 0.60),
    (16, 0.35),
    (21, 0.10),
    (25, 0.05),
)

#: API30 mm -> moisture factor, linear between the knots.  Bands would give
#: 28 mm and 36 mm the same multiplier and then jump by a third at one
#: edge; the wetter of two places must always score higher.
CHANCE_MOISTURE_RAMP = ((10, 0.5), (20, 0.8), (30, 1.15), (45, 1.25))

#: No usable API30 number: no moisture evidence either way.
CHANCE_MOISTURE_UNKNOWN = 1.0

#: The API30 temperature gate is not satisfied (or is unknown).
CHANCE_TEMPERATURE_FAILED = 0.6

#: Frost within the completed station week.
CHANCE_FROST = 0.4

#: HoubyMapa score ``s`` (0..1) -> ``base + span * s``.  Both map factors
#: are a correction of the *place*, applied to every day of the horizon:
#: neither map publishes a forecast, but what they mostly encode -- terrain
#: and soil -- does not change over two weeks, and it is the ranking
#: between locations that the chance is read for.
CHANCE_HOUBYMAPA_BASE = 0.85
CHANCE_HOUBYMAPA_SPAN = 0.3

#: ČHMÚ level -> ``base + step * (level - pivot)``.
CHANCE_CHMI_BASE = 0.9
CHANCE_CHMI_STEP = 0.05
CHANCE_CHMI_PIVOT = 3.0

#: Lead time in days (``day - today``) -> damping factor, linear between
#: the knots.  Open-Meteo has run ~1.9x wetter than the station at these
#: points, so a number six days out must not read like a measurement.
#: Past days clamp to the first knot, i.e. no damping at all.
CHANCE_HORIZON_DAMPING = ((0, 1.0), (3, 1.0), (7, 0.9), (16, 0.75))

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


def _ramp_text(knots: tuple[tuple[float, float], ...], unit: str = "") -> str:
    """``0 дн→×0.05, 5 дн→×0.15`` -- a ramp spelled out for a human."""
    return ", ".join(f"{x:g}{unit}→×{y:g}" for x, y in knots)


def interpretation_guide() -> str:
    """Human guidance generated from the same constants as the rules."""
    dry, moderate, wet = API30_BANDS_MM
    return f"""Как читать (справка, стабильный текст):
1. API30 — сумма осадков за {API30_WINDOW_DAYS} дней с затуханием {API30_DECAY}/сут (формула ČHMÚ, воспроизведена точно).
2. Ориентировочные полосы API30 для чтения глазами, НЕ откалиброваны: <{dry:g} мм сухо, {dry:g}–{moderate:g} мм умеренно, {moderate:g}–{wet:g} мм хорошо, >{wet:g} мм очень влажно. В расчёте шанса эти полосы не используются: там влага входит плавной кривой из п. 6, поэтому узлы там другие.
3. Рабочий порог API30 {API30_THRESHOLD_MM:g} мм пока не перекалиброван.
4. Дождевой эпизод: ≥{RAIN_EPISODE_MM:g} мм за {RAIN_EPISODE_DAYS} календарных дня при средней температуре {RAIN_T_MEAN_MIN:g}–{RAIN_T_MEAN_MAX:g} °C; предполагаемое окно D+{GROWTH_WINDOW_FROM_DAYS}...D+{GROWTH_WINDOW_TO_DAYS}.
5. Высокая вероятность допустима только внутри основного окна D+{GROWTH_WINDOW_FROM_DAYS}...D+{GROWTH_WINDOW_TO_DAYS}: до него свежий дождь даёт максимум среднюю. После него до D+{GROWTH_RESIDUAL_TO_DAYS} сохраняется остаточная вероятность максимум средней силы. Дополнительно для высокой нужны свежий API30 ≥{API30_THRESHOLD_MM:g} мм, средняя температура {API30_T_MEAN_MIN:g}–{API30_T_MEAN_MAX:g} °C, минимум > {API30_T_MIN_ABOVE:g} °C, достаточная история, отсутствие заморозка за 7 завершённых станционных суток и — только для сегодняшнего дня — свежая высокая поддержка хотя бы одной карты. Карты прогноза не публикуют, поэтому в вердикте на будущие дни их уровень не влияет ни в плюс, ни в минус (в проценте шанса они учитываются иначе — см. п. 6).
6. Шанс в процентах — сравнимое между локациями число, НЕ вероятность находки; все составляющие перемножаются: (а) фаза — плавная кривая по числу дней от пика дождя ({_ramp_text(CHANCE_PHASE_RAMP, " дн")}), между узлами линейно, при нескольких эпизодах берётся максимум; (б) влага — плавная кривая по API30 ({_ramp_text(CHANCE_MOISTURE_RAMP, " мм")}), без пригодного числа влага нейтральна ×{CHANCE_MOISTURE_UNKNOWN:g}; (в) ×{CHANCE_TEMPERATURE_FAILED:g} при невыполненном температурном условии и ×{CHANCE_FROST:g} при заморозке; (г) поправка места по сегодняшним картам — HoubyMapa ×({CHANCE_HOUBYMAPA_BASE:g}+{CHANCE_HOUBYMAPA_SPAN:g}·score), ČHMÚ ×({CHANCE_CHMI_BASE:g}+{CHANCE_CHMI_STEP:g}·(уровень−{CHANCE_CHMI_PIVOT:g})) — применяется ко всем дням горизонта: карты описывают в основном рельеф и почву, и относительный порядок мест держится дольше одного дня; устаревшая карта не участвует вовсе; (д) поправка на дальность прогноза ({_ramp_text(CHANCE_HORIZON_DAMPING, " дн")}). Округление до {CHANCE_STEP} %, диапазон {CHANCE_MIN}–{CHANCE_MAX} %; без свежей станции число ограничено {CHANCE_NO_STATION_CAP} %. Кривые непрерывны, поэтому по одному слову фазы число дня уже не восстанавливается — слово осталось только для формулировки.
7. Карта ČHMÚ — модельный ориентир по микоризным видам (hřib, kozák, liška), а не доказательство наличия грибов; опята и дереворазрушающие виды она не описывает.
8. Прогноз Open-Meteo в этих точках исторически был примерно в 1.9 раза «мокрее» станции; дальше 7 дней — ненадёжно, говорить «ориентировочно».
9. HoubyMapa — независимая модель (радар ČHMÚ + Open-Meteo, влажность и температура почвы); совпадение с ČHMÚ усиливает вывод, расхождение требует осторожности.
10. Полные станционные сутки главнее модели; неполные сутки — нижняя оценка и не складываются с модельным днём.
11. Данные: zdroj ČHMÚ (CC BY 4.0), HoubyMapa.cz, Open-Meteo (CC BY 4.0)."""
