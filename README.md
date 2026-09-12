# mushroom-alerts

Daily mushroom-growth signal for a handful of places around Valašské
Meziříčí, assembled from the ČHMÚ growth-probability raster, HoubyMapa, ČHMÚ
station open data and Open-Meteo -- plus an API30 rain index projected 16
days ahead, which none of the upstreams offers.  No bot of its own: it is a
CLI that Hermes Agent runs from cron.  `brief` prints the facts and Hermes
turns them into a sentence ("how likely now, and when does it become
likely"); `check` keeps the deterministic exit-code contract for when no
interpretation is wanted.  Nothing here calls an LLM.  See `PLAN.md` for
the whole design, `hermes/PROMPT.md` for the text pasted into the Hermes
cron job, `mushroom_alerts/base.py` for the contract every source module
follows, and `mushroom_alerts/rules.py` for the triggers.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Run

```sh
.venv/bin/python -m mushroom_alerts check            # fetch, store, decide
.venv/bin/python -m mushroom_alerts check --json     # machine-readable
.venv/bin/python -m mushroom_alerts check --only chmi_map,houbymapa

.venv/bin/python -m mushroom_alerts brief            # facts for Hermes to read
.venv/bin/python -m mushroom_alerts brief --json
.venv/bin/python -m mushroom_alerts brief --days 7   # shorter forecast table

.venv/bin/python -m mushroom_alerts status           # last stored snapshot
.venv/bin/python -m mushroom_alerts status --json

.venv/bin/python -m mushroom_alerts calibration         # bias/MAE, read-only
.venv/bin/python -m mushroom_alerts calibration --json

.venv/bin/python -m mushroom_alerts list
.venv/bin/python -m mushroom_alerts add "Rožnov pod Radhoštěm" 49.4586 18.1436
.venv/bin/python -m mushroom_alerts del "Rožnov pod Radhoštěm"
```

Typical output:

```
$ python -m mushroom_alerts status
🍄 Valašské Meziříčí: ČHMÚ 3/5, HoubyMapa 3/5 (0.47), станция API30 20 mm, SRA 3d 0.2 mm, T 14.2 °C · прогноз: API30 сегодня 19 mm, max 35 mm 23.9., дождь 9.8 mm 10.9., порог 25 mm пройден 22.9.

$ python -m mushroom_alerts check; echo $?
🍄 Valašské Meziříčí: ČHMÚ 3/5, HoubyMapa 3/5 (0.47), станция API30 20 mm, SRA 3d 0.2 mm, T 14.2 °C · прогноз: API30 ≥ 25 mm с 22.9. (пик 35 mm 23.9., дождь 10 mm 22.9.) — ориентировочно
10
```

## Exit-code contract (PLAN §2)

`check` uses the deterministic signal contract:

| code | meaning | what Hermes does |
|---|---|---|
| `0` | nothing new, stay quiet | nothing |
| `10` | positive signal emitted to stdout | Hermes may forward stdout |
| `1` | error | report an error, at most once a day |

Exit `1` means the script or rule calculation failed, or **every** source
failed. One dead source or one failed location is a partial result: useful
data is retained, the failure is explicit, and the command exits `0` unless
a positive `check` signal uses `10`.

`brief`, `status`, `list`, `add` and `del` exit `0` on success and `1` on a
usage error.  With no trigger firing, `check` still prints the snapshot (handy by
hand, ignored by Hermes) and exits `0`.  Re-running `check` on the same day
is idempotent: same text, same exit code, no duplicate emission rows. An
emission row means the signal was printed, not that Telegram confirmed it.

## The Hermes brief

```sh
.venv/bin/python -m mushroom_alerts brief
```

Runs the same fetch/store/derive/evaluate/render pipeline as `check`, then prints ~3 KB per
location of plain facts in Russian: ČHMÚ map level with its change against
yesterday and a week ago, HoubyMapa, station API30 / SRA over 1-3-7-30 days
/ temperatures / soil / humidity, a 14-day history table, a 16-day forecast
table with the derived API30 curve and its threshold crossing, 7-day
temperature coverage, frost, the rain episode and D+7...D+12 window, API30
dynamics and input quality, the
deterministic triggers that fired, the sources that failed, and a stable
"Как читать" cheat sheet.  It draws **no** conclusion -- that is Hermes
Agent's job; paste `hermes/PROMPT.md` into its 08:30 cron task.  `--days N`
shortens the forecast table, `--json` gives the same content as a dict
(including `check --json`'s per-location decision data).  Exit code is
always `0` unless every source failed or rule calculation failed. Running
`brief` archives observations and forecast releases but does not update
antispam/emission state.

`calibration [--json]` compares archived Open-Meteo rainfall and projected
API30 with later station observations. It reports sample size, bias
(`forecast - observation`) and MAE per location for horizons 1-3, 4-7 and
8-16 days. It is read-only and never adjusts data, thresholds or forecasts.

## Configuration

| env var | default | meaning |
|---|---|---|
| `MUSHROOM_DB` | `./state.sqlite` | SQLite state (history, forecast runs, params, emissions) |
| `MUSHROOM_LOCATIONS` | `./locations.yaml` | watched locations |
| `MUSHROOM_RECORD` | unset | `1` dumps every HTTP response into `tests/fixtures/` |
| `MUSHROOM_API30_THRESHOLD` | `25` (mm) | API30 level that trigger 4 announces; provisional, see PLAN §3 |

`locations.yaml` holds only name + coordinates.  Everything derived (ČHMÚ
pixel, HoubyMapa cell, nearest stations) is computed on the fly and cached
in SQLite, and is dropped automatically when a location moves.

## Tests

```sh
.venv/bin/python -m pytest
```

Fully offline (297 tests at the end of the refactor): `tests/fixtures/` holds real responses recorded on
2026-09-07 with `MUSHROOM_RECORD=1`.  To refresh them:

```sh
MUSHROOM_RECORD=1 .venv/bin/python -m mushroom_alerts check
```

## Sources

* ČHMÚ "Pravděpodobnost růstu hub" raster — `data-provider.chmi.cz`, level
  1..5 sampled at the location's pixel.  Data © ČHMÚ, CC BY 4.0.
* HoubyMapa.cz `/predikce` — 0.2° grid, nearest cell's level and score.
* ČHMÚ open data — daily station SRA/API30/T (and 10-minute rain for today).
  Official and stable; CC BY 4.0.
* Open-Meteo forecast API — 16 days of daily rain and temperature, the input
  to the projected API30 curve.  CC BY 4.0.

The first two are undocumented endpoints and may change without notice;
failures are soft by design.

## Data and delivery semantics

- ČHMÚ station days and Open-Meteo calendar days are joined conservatively.
  A complete station day wins; an incomplete station day is displayed as a
  lower bound but does not replace or add to a whole model day.
- Forecast releases are archived in additive `forecast_runs` and
  `forecast_points` tables. Legacy `forecasts` remains dual-written for
  rollback compatibility. Metrics in one view always come from one release.
- SQLite migrations use `PRAGMA user_version`; current schema version is 2.
- The application, not Hermes, calculates the conservative biological
  verdict. A qualifying rain episode starts a D+7...D+12 growth window;
  moisture above the API30 threshold before D+7 can produce at most a
  medium verdict. High additionally requires fresh API30 and forecast data,
  a valid temperature gate, sufficient history without frost, and fresh
  high support from at least one map.
- Hermes keeps daily and Friday verdicts in independent durable notepads.
  The prompt must address the notepad by the actual cron job ID, not by the
  human-readable job name.
  Transport results remain in Hermes `delivery_outcome`; they are not copied
  into the application database. Exactly-once delivery is not guaranteed.
