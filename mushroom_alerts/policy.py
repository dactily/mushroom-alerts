"""Central, versioned rule parameters used by calculations and documentation."""

RULES_VERSION = "2"

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
NEXT_RAIN_MM = 5.0

BIOLOGICAL_MAP_LEVEL = 4.0
BIOLOGICAL_VERDICT_LABELS = {
    "insufficient": "недостаточно данных",
    "low": "низкая",
    "medium": "средняя",
    "high": "высокая",
}


def interpretation_guide() -> str:
    """Human guidance generated from the same constants as the rules."""
    dry, moderate, wet = API30_BANDS_MM
    return f"""Как читать (справка, стабильный текст):
1. API30 — сумма осадков за {API30_WINDOW_DAYS} дней с затуханием {API30_DECAY}/сут (формула ČHMÚ, воспроизведена точно).
2. Ориентировочные полосы API30, НЕ откалиброваны: <{dry:g} мм сухо, {dry:g}–{moderate:g} мм умеренно, {moderate:g}–{wet:g} мм хорошо, >{wet:g} мм очень влажно.
3. Рабочий порог API30 {API30_THRESHOLD_MM:g} мм пока не перекалиброван.
4. Дождевой эпизод: ≥{RAIN_EPISODE_MM:g} мм за {RAIN_EPISODE_DAYS} календарных дня при средней температуре {RAIN_T_MEAN_MIN:g}–{RAIN_T_MEAN_MAX:g} °C; предполагаемое окно D+{GROWTH_WINDOW_FROM_DAYS}...D+{GROWTH_WINDOW_TO_DAYS}.
5. Высокая вероятность допустима только внутри окна D+{GROWTH_WINDOW_FROM_DAYS}...D+{GROWTH_WINDOW_TO_DAYS}: до него свежий дождь даёт максимум среднюю. Дополнительно нужны свежий API30 ≥{API30_THRESHOLD_MM:g} мм, средняя температура {API30_T_MEAN_MIN:g}–{API30_T_MEAN_MAX:g} °C, минимум > {API30_T_MIN_ABOVE:g} °C, достаточная история без заморозка и свежая высокая поддержка хотя бы одной карты.
6. Карта ČHMÚ — модельный ориентир по микоризным видам (hřib, kozák, liška), а не доказательство наличия грибов; опята и дереворазрушающие виды она не описывает.
7. Прогноз Open-Meteo в этих точках исторически был примерно в 1.9 раза «мокрее» станции; дальше 7 дней — ненадёжно, говорить «ориентировочно».
8. HoubyMapa — независимая модель (радар ČHMÚ + Open-Meteo, влажность и температура почвы); совпадение с ČHMÚ усиливает вывод, расхождение требует осторожности.
9. Полные станционные сутки главнее модели; неполные сутки — нижняя оценка и не складываются с модельным днём.
10. Данные: zdroj ČHMÚ (CC BY 4.0), HoubyMapa.cz, Open-Meteo (CC BY 4.0)."""
