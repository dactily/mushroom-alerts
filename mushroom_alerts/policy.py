"""Central, versioned rule parameters used by calculations and documentation."""

RULES_VERSION = "7"

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

#: A day counts as wet from this much rain.  Below it the day is dry; a day
#: with no number at all is neither (see ``biology._wet_spells``).
RAIN_WET_DAY_MM = 1.0

#: How many dry days may sit inside one wet spell.  One: a shower that
#: pauses for a day is still one spell, two dry days end it.
RAIN_EPISODE_GAP_DAYS = 1

#: The second, gentler episode type: a **soak**.  The pulse above only sees
#: a downpour, and around Liberec on 2026-09-13 the station had API30
#: 26–32 mm built out of drizzle -- best three-day window 7 mm, 20 mm of
#: rain over three weeks -- so the ground was wet and not one episode
#: qualified.  A soak is API30 at or above :data:`RAIN_SOAK_API30_MM` held
#: for :data:`RAIN_SOAK_DAYS` days in a row.
#:
#: The line is *below* :data:`API30_THRESHOLD_MM` on purpose: a slow soak
#: never spikes, so demanding the pulse threshold would rule it out by
#: definition.  Seven days is what makes it a soak rather than the tail of
#: a downpour -- a single 30 mm rain decays below 20 mm in under six days,
#: so a week above the line needs rain that kept coming.
#:
#: Soaks feed the **chance only**.  The categorical verdict of
#: ``biology.assess_day`` is a safety gate and still wants a real rain
#: episode, and :data:`RAIN_EPISODE_MM` / :data:`RAIN_EPISODE_DAYS` were
#: re-examined against the 2026-09-13 country sample and deliberately left
#: where they are: loosening them would have loosened the gate too.
RAIN_SOAK_API30_MM = 20.0
RAIN_SOAK_DAYS = 7

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
# Calibrated once, on 2026-09-13, against the ČHMÚ map over 75 points
# spread across the whole country (``tools/sample_chmu.py``): it says "how
# much this place today looks like the conditions under which mushrooms
# come", not a measured find frequency, and the anchors it was fitted to
# are somebody else's model, not a count of baskets.
# ----------------------------------------------------------------------

#: What a place scores when moisture, the fruiting window and both maps all
#: say "yes".  Deliberately short of 100: nothing in this model has earned
#: certainty, and :data:`CHANCE_MAX` still trims the rest.
CHANCE_FULL_PCT = 90.0

#: How the three contributions share the number.  They are a **weighted
#: mean**, not factors: the old model multiplied the phase in, so a place
#: whose rain episode had expired was multiplied by 0.05 and collapsed to
#: the floor however soaked the ground was.  Measured on the country
#: sample, that put seven of the eight strongest ČHMÚ zones at 5–20 %.
#: Now moisture alone can carry a number and the window is a contribution.
#:
#: A contribution with no data drops out and the remaining weights are
#: renormalised, so a location whose maps are stale stays comparable with
#: one whose maps are fresh.  Moisture is the exception: without a usable
#: API30 there is no percentage at all (see :mod:`~mushroom_alerts.chance`).
CHANCE_WEIGHT_MOISTURE = 0.40
CHANCE_WEIGHT_PHASE = 0.30
CHANCE_WEIGHT_MAP = 0.30

#: How many days the moisture term averages over, ending on the day judged.
#: A single day's API30 barely separates the ČHMÚ levels at all (Spearman
#: 0.32 over the sample; medians 22 / 27 / 28 / 32 mm for levels 2–5),
#: because a first rain on ground that has been dry for weeks reads the
#: same as steady wetness.  The week mean separates them (0.70; 15 / 22 /
#: 24 / 33 mm) and is the mycological statement too: what fruits is ground
#: that has *stayed* wet.
CHANCE_MOISTURE_DAYS = 7

#: Mean API30 over that week, in mm -> moisture term 0..1, linear between
#: the knots and clamped outside them.  Bands would give 28 mm and 36 mm
#: the same value and then jump by a third at one edge; the wetter of two
#: places must always score higher.
CHANCE_MOISTURE_RAMP = ((10, 0.05), (25, 0.55), (40, 1.00))

#: Days since a rain anchor -> phase term 0..1, linear between the knots.
#: A stand does not switch on the morning D+7 and off the evening D+12; it
#: ramps up over days, holds, and fades.  The plateau covers the same
#: primary window as
#: :data:`GROWTH_WINDOW_FROM_DAYS`..:data:`GROWTH_WINDOW_TO_DAYS`, and the
#: tail reaches its floor a few days past :data:`GROWTH_RESIDUAL_TO_DAYS`,
#: where the verdict already calls the window expired.  The floor is 0.15
#: and not 0: "no fresh trigger" is a statement about the timing, and the
#: other two contributions are still entitled to their say.
CHANCE_PHASE_RAMP = (
    (0, 0.10),
    (5, 0.30),
    (7, 1.00),
    (12, 1.00),
    (16, 0.60),
    (21, 0.30),
    (25, 0.15),
)

#: The API30 temperature gate is not satisfied (or is unknown).
CHANCE_TEMPERATURE_FAILED = 0.6

#: Frost within the completed station week.
CHANCE_FROST = 0.4

#: ČHMÚ level 1..5 -> its half of the map term, 0..1.  HoubyMapa enters
#: through its own score, which is already 0..1 and is **not** rescaled to
#: agree with ČHMÚ: it is an independent model and its disagreement is the
#: point.  The map term is the mean of whichever of the two is fresh.
#:
#: Both are a correction of the *place*, applied to every day of the
#: horizon: neither map publishes a forecast, but what they mostly encode
#: -- terrain and soil -- does not change over two weeks, and it is the
#: ranking between locations that the chance is read for.
CHANCE_CHMI_RAMP = ((1.0, 0.0), (5.0, 1.0))

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


def _ramp_text(
    knots: tuple[tuple[float, float], ...], unit: str = "", mul: bool = False
) -> str:
    """``0 дн→0.10, 5 дн→0.30`` -- a ramp spelled out for a human.

    ``mul=True`` prefixes the value with ``×``: the horizon damping really
    is a multiplier, while the three contributions of the chance are terms
    of a weighted mean and an ``×`` in front of them would be a lie.
    """
    sign = "→×" if mul else "→"
    return ", ".join(f"{x:g}{unit}{sign}{y:g}" for x, y in knots)


def interpretation_guide() -> str:
    """Human guidance generated from the same constants as the rules."""
    dry, moderate, wet = API30_BANDS_MM
    return f"""Как читать (справка, стабильный текст):
1. API30 — сумма осадков за {API30_WINDOW_DAYS} дней с затуханием {API30_DECAY}/сут (формула ČHMÚ, воспроизведена точно).
2. Ориентировочные полосы API30 для чтения глазами, НЕ откалиброваны: <{dry:g} мм сухо, {dry:g}–{moderate:g} мм умеренно, {moderate:g}–{wet:g} мм хорошо, >{wet:g} мм очень влажно. В расчёте шанса эти полосы не используются: там влага входит плавной кривой из п. 6, поэтому узлы там другие.
3. Рабочий порог API30 {API30_THRESHOLD_MM:g} мм пока не перекалиброван.
4. Дождевой эпизод (ливень): подряд идущие влажные дни (≥{RAIN_WET_DAY_MM:g} мм за сутки; внутри эпизода допускается не больше {RAIN_EPISODE_GAP_DAYS} сухого дня, два сухих дня его закрывают), если где-то внутри набирается ≥{RAIN_EPISODE_MM:g} мм за {RAIN_EPISODE_DAYS} календарных дня при средней температуре {RAIN_T_MEAN_MIN:g}–{RAIN_T_MEAN_MAX:g} °C. День без данных не влажный и не сухой: эпизод он не рвёт. Два дождя, разделённые сухими днями, остаются двумя эпизодами со своими пиками; предполагаемое окно каждого — D+{GROWTH_WINDOW_FROM_DAYS}...D+{GROWTH_WINDOW_TO_DAYS} от его пика.
4a. Второй, мягкий тип эпизода — промокание: API30 ≥ {RAIN_SOAK_API30_MM:g} мм {RAIN_SOAK_DAYS} дней подряд. Морось неделями не даёт ливневого эпизода вовсе (у Либерца API30 26–32 мм при лучшем трёхдневном окне 7 мм), а земля при этом мокрая. Точки отсчёта промокания — каждый его день начиная с {RAIN_SOAK_DAYS}-го: участок считается «запущенным» в тот день, когда он уже неделю держит уровень, и заново каждый следующий день, пока держит. Поэтому длинное промокание даёт открытое окно всё время, пока оно длится, а после конца стареет так же, как ливень. Порог {RAIN_SOAK_API30_MM:g} мм ниже рабочего порога {API30_THRESHOLD_MM:g} мм намеренно: медленное промокание не даёт всплеска. Промокания входят только в процент шанса; категорический вердикт по-прежнему требует настоящего ливневого эпизода — это предохранитель, и его пороги оставлены как были.
5. Высокая вероятность допустима только внутри основного окна D+{GROWTH_WINDOW_FROM_DAYS}...D+{GROWTH_WINDOW_TO_DAYS}: до него свежий дождь даёт максимум среднюю. После него до D+{GROWTH_RESIDUAL_TO_DAYS} сохраняется остаточная вероятность максимум средней силы. Дополнительно для высокой нужны свежий API30 ≥{API30_THRESHOLD_MM:g} мм, средняя температура {API30_T_MEAN_MIN:g}–{API30_T_MEAN_MAX:g} °C, минимум > {API30_T_MIN_ABOVE:g} °C, достаточная история, отсутствие заморозка за 7 завершённых станционных суток и — только для сегодняшнего дня — свежая высокая поддержка хотя бы одной карты. Карты прогноза не публикуют, поэтому в вердикте на будущие дни их уровень не влияет ни в плюс, ни в минус (в проценте шанса они учитываются иначе — см. п. 6).
6. Шанс в процентах — сравнимое между локациями число, НЕ вероятность находки. Считается как взвешенное среднее трёх слагаемых (каждое 0..1), умноженное на {CHANCE_FULL_PCT:g} % и на поправки: (а) влага, вес {CHANCE_WEIGHT_MOISTURE:g} — средний API30 за {CHANCE_MOISTURE_DAYS} дней по кривой ({_ramp_text(CHANCE_MOISTURE_RAMP, " мм")}), между узлами линейно; неделя, а не один день, потому что плодоносит земля, которая остаётся мокрой, а не та, на которую впервые за месяц пролился дождь; (б) фаза, вес {CHANCE_WEIGHT_PHASE:g} — плавная кривая по числу дней от точки отсчёта эпизода ({_ramp_text(CHANCE_PHASE_RAMP, " дн")}), при нескольких эпизодах берётся максимум; учитываются оба типа эпизодов из пп. 4 и 4a; (в) карты, вес {CHANCE_WEIGHT_MAP:g} — среднее по свежим картам: ČHMÚ уровень 1..5 линейно в {_ramp_text(CHANCE_CHMI_RAMP)} и score HoubyMapa как есть; score НЕ подгоняется под ČHMÚ — это независимая модель, и её несогласие имеет цену. Карты применяются ко всем дням горизонта: они описывают в основном рельеф и почву, и относительный порядок мест держится дольше одного дня. Слагаемое без данных выпадает, веса пересчитываются по оставшимся, поэтому локация без свежих карт остаётся сравнимой. Влага не выпадает никогда: без пригодного API30 на этот день (числа нет, или источник устарел/отсутствует) процент не считается вовсе — в отчёте «нет данных», а не маленькое число; для сегодняшнего дня, если расчётная кривая непригодна, берётся собственный API30 станции, поэтому устаревший прогнозный выпуск лишает числа будущие дни, а не измеренный сегодняшний. Дальше сумму умножают: ×{CHANCE_TEMPERATURE_FAILED:g} при невыполненном температурном условии, ×{CHANCE_FROST:g} при заморозке, и поправка на дальность прогноза ({_ramp_text(CHANCE_HORIZON_DAMPING, " дн", mul=True)}). Округление до {CHANCE_STEP} %, диапазон {CHANCE_MIN}–{CHANCE_MAX} %; без свежей станции число ограничено {CHANCE_NO_STATION_CAP} %. Кривые непрерывны, поэтому по одному слову фазы число дня уже не восстанавливается — слово осталось только для формулировки.
6a. Веса и узлы подобраны один раз, 13.09.2026, по 75 точкам по всей стране (`tools/sample_chmu.py`) против ориентиров «уровень ČHMÚ → процент»: 1→10, 2→25, 3→45, 4→65, 5→85. Совпадение намеренно неполное: Спирмен 0.82, средняя ошибка 9.9 пункта. Свои измерения (влага и фаза, без карт вовсе) дают 0.73 — то есть согласие в основном наше, а не заимствованное; если бы в карты входил один ČHMÚ, было бы 0.87, и HoubyMapa мы усредняем именно затем, чтобы не стать его копией. Там, где дождь прошёл вчера, наше число ниже ČHMÚ (окно ещё не открылось), а внутри свежего окна — выше.
7. Карта ČHMÚ — модельный ориентир по микоризным видам (hřib, kozák, liška), а не доказательство наличия грибов; опята и дереворазрушающие виды она не описывает.
8. Прогноз Open-Meteo в этих точках исторически был примерно в 1.9 раза «мокрее» станции; дальше 7 дней — ненадёжно, говорить «ориентировочно».
9. HoubyMapa — независимая модель (радар ČHMÚ + Open-Meteo, влажность и температура почвы); совпадение с ČHMÚ усиливает вывод, расхождение требует осторожности.
10. Полные станционные сутки главнее модели; неполные сутки — нижняя оценка и не складываются с модельным днём.
11. Данные: zdroj ČHMÚ (CC BY 4.0), HoubyMapa.cz, Open-Meteo (CC BY 4.0)."""
