# mushroom-alerts

Daily mushroom-growth signal for a handful of places around Valašské
Meziříčí, assembled from the ČHMÚ growth-probability raster, HoubyMapa, ČHMÚ
station open data and Open-Meteo -- plus an API30 rain index projected 16
days ahead, which none of the upstreams offers.  No bot of its own: it is a
CLI that Hermes Agent runs from cron.  `brief --mode daily|weekend` prints a
short block that is already worded for a human and already says whether to
send it at all; Hermes only re-words it for Telegram.  The block is a list of
"location — chance, %" in the order of `locations.yaml`: the script never
ranks or recommends, the human decides where to drive.  Plain `brief` prints
every fact and both tables for debugging, and `check` keeps the deterministic
exit-code contract for when no interpretation is wanted.  Nothing here calls an LLM.  See `PLAN.md` for
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

.venv/bin/python -m mushroom_alerts brief --mode daily    # ready block + send flag
.venv/bin/python -m mushroom_alerts brief --mode weekend  # weekend plan, always sent
.venv/bin/python -m mushroom_alerts brief                 # full debug view
.venv/bin/python -m mushroom_alerts brief --json
.venv/bin/python -m mushroom_alerts brief --days 7        # shorter forecast table
.venv/bin/python -m mushroom_alerts brief --location valmez  # one location only

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
🍄 Valašské Meziříčí: ČHMÚ 3/5, HoubyMapa 3/5 (0.47), станция API30 20 mm, SRA 3d 0.2 mm, T 14.2 °C · вердикт средняя · прогноз: API30 сегодня 19 mm, max 35 mm 23.9., дождь 9.8 mm 10.9., порог 25 mm пройден 22.9.

$ python -m mushroom_alerts check; echo $?
🍄 Valašské Meziříčí: ČHMÚ 3/5, HoubyMapa 3/5 (0.47), станция API30 20 mm, SRA 3d 0.2 mm, T 14.2 °C · вердикт средняя · прогноз: API30 ≥ 25 mm с 22.9. (пик 35 mm 23.9., дождь 10 mm 22.9.) — ориентировочно
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
hand, ignored by Hermes) and exits `0`. Re-running `check` with identical
source data is storage-idempotent: readings are upserted, the forecast release
reuses its content-derived `run_id`, and emission rows are not duplicated. The
fetch requests still happen, and a same-day retry deliberately prints the same
text and exit code so Hermes can retry transport. An emission row means the
signal was printed, not that Telegram confirmed it.

## The Hermes brief

```sh
.venv/bin/python -m mushroom_alerts brief
```

Runs the same fetch/store/derive/evaluate/render pipeline as `check`, then prints ~3 KB per
location of plain facts in Russian: ČHMÚ map level with its change against
yesterday and a week ago, HoubyMapa, station API30 / SRA over 1-3-7-30 days
/ temperatures / soil / humidity, a 14-day history table, a 16-day forecast
table with the derived API30 curve and its threshold crossing, 7-day
temperature coverage, frost, all distinct rain episodes, their D+7...D+12
primary and D+13...D+21 residual phases, API30 dynamics and input quality, the
deterministic triggers that fired, the sources that failed, and a stable
"Как читать" cheat sheet. That view is for debugging.

What the cron jobs actually run is `brief --mode daily` or `--mode weekend`:
no tables, Russian, with `ОТПРАВЛЯТЬ: да|нет` on the first line, and under
2 KB even with twenty locations. It prints `ШАНС` (one line per location, in
the order of `locations.yaml`), `ФАЗА`, `ПОДРОБНО` for the two locations with
the best chance, and `ОГОВОРКИ`. The decision compares today against the last
report of an earlier date stored in `reports`; the weekend plan is always
sent. `--mode --json` prints the same fields as a dict. `--days N`
shortens the forecast table, `--location SLUG` (repeatable) narrows either
view to a few locations -- a debugging filter, so with `--mode` it prints the
decision but records no report row. `--json` gives the same content as a dict
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

`locations.yaml` holds name + coordinates, plus an optional `short` (the
name the message prints -- "Bystřice pod Hostýnem" does not fit a list of
twenty) and an optional `slug`.  Its order is the order of the message and is
preserved everywhere.  Everything derived (ČHMÚ pixel, HoubyMapa cell,
nearest stations) is computed on the fly and cached in SQLite, and is dropped
automatically when a location moves.

## Tests

```sh
.venv/bin/python -m pytest
```

Fully offline (430 tests): `tests/fixtures/` holds real responses recorded on
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
  Schema v3 adds a stable content hash: identical reruns reuse the same release,
  while changed values on the same day create a new one.
- SQLite migrations use `PRAGMA user_version`; current schema version is 5.
  Schema v4 adds the additive `reports` table (one row per date and mode);
  schema v5 adds the additive `reports.chances_json` column, the per-location
  percentages the send rule compares.
- The application, not Hermes, calculates both the comparable chance in
  percent (PLAN §9b: a ramp over the days since the rain anchor, the best
  over all episodes, x a ramp over API30 x temperature gate x frost x both
  maps, applied to every day as a correction of the place, x a lead-time
  damping; rounded to 5 % inside 5-95 %, capped at 50 % without a fresh
  station). A day with no usable API30 -- no number, or one from a stale or
  missing release -- gets no percentage at all: the block says «нет данных»
  and names the location under `ОГОВОРКИ`, because every neutral stand-in
  sits above part of the moisture ramp and would let lost data raise the
  result. Today falls back to the station's own API30 when only the forecast
  release went stale. Alongside it stands the conservative biological
  verdict. All qualifying rain episodes are retained: a rain episode is a run
  of wet days (>= 1 mm), tolerating a single dry day inside it, that reaches
  20 mm over some 3-day window at 12-22 °C; two dry days end it, so two rains
  a week apart stay two episodes with two anchors. A day with no measurement
  is neither wet nor dry and never splits a spell. An older active episode
  outranks a newer waiting episode. D+7...D+12 is the primary window, D+13...D+21 is a
  residual phase capped at medium. Moisture before D+7 is also capped at
  medium. High additionally requires fresh API30 and forecast data, a valid
  temperature gate, sufficient history, no frost over the seven completed
  station days, and fresh high support from at least one map.
- The application also decides whether to send at all. `brief --mode
  daily|weekend` prints a short ready-to-send Russian block starting with
  `ОТПРАВЛЯТЬ: да|нет`, and stores the state it compared against in the
  `reports` table. Daily it speaks when a location's chance moved by 10
  points or more, when the best chance crossed 60 % either way, on the first
  run, when a location is new, when a location gained or lost its number
  («нет данных» on both sides is not news), or when the error class
  changed. Hermes only
  re-words that block; durable notepads are no longer used.
  Transport results remain in Hermes `delivery_outcome`; they are not copied
  into the application database. Exactly-once delivery is not guaranteed.

The v3 column and index are additive and accept the v2 insert shape, but the
v2 binary deliberately rejects a higher `user_version`. Therefore an immediate
rollback to a v2 commit is not a SHA switch alone: while jobs are stopped, set
`PRAGMA user_version=2` on the backed-up database, then switch code. The column
and index stay in place and no rows are removed. Rolling forward to v3 detects
the existing column and restores `user_version=3`.

Schema v4 is the same kind of step: the new `reports` table is additive, and a
rollback to a v3 binary needs `PRAGMA user_version=3` while the jobs are
stopped. The table itself may stay; no other table is touched. Schema v5 adds
only `reports.chances_json` (default `'{}'`) and rolls back the same way with
`PRAGMA user_version=4`; a v4 binary ignores the extra column, and the first
v5 run after such a rollback sends once, because it finds no stored chances.
