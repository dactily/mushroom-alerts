# mushroom-alerts

Daily mushroom-growth signal for a handful of places around Valašské
Meziříčí, assembled from the ČHMÚ growth-probability raster, HoubyMapa, ČHMÚ
station open data and Open-Meteo -- plus an API30 rain index projected 16
days ahead, which none of the upstreams offers.  No LLM, no bot of its own:
it is a CLI that Hermes runs from cron and whose stdout it forwards to
Telegram.  See `PLAN.md` for the whole design, `mushroom_alerts/base.py` for
the contract every source module follows, and `mushroom_alerts/rules.py` for
the triggers.

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
.venv/bin/python -m mushroom_alerts status           # last stored snapshot
.venv/bin/python -m mushroom_alerts status --json

.venv/bin/python -m mushroom_alerts list
.venv/bin/python -m mushroom_alerts add "Rožnov pod Radhoštěm" 49.4586 18.1436
.venv/bin/python -m mushroom_alerts del "Rožnov pod Radhoštěm"
```

Typical output:

```
$ python -m mushroom_alerts status
🍄 Valašské Meziříčí: ČHMÚ 3/5, HoubyMapa 3/5 (0.47), stanice API30 20 mm, SRA 3d 0.2 mm, T 14.2 °C · prognóza: API30 dnes 19 mm, max 35 mm 23.9., déšť 9.8 mm 10.9., práh 25 mm překročen 22.9.

$ python -m mushroom_alerts check; echo $?
🍄 Valašské Meziříčí: ČHMÚ 3/5, HoubyMapa 3/5 (0.47), stanice API30 20 mm, SRA 3d 0.2 mm, T 14.2 °C · prognóza: API30 ≥ 25 mm od 22.9. (vrchol 35 mm 23.9., déšť 10 mm 22.9.) — orientačně
10
```

## Exit-code contract (PLAN §2)

`check` is the only command with a meaningful exit code; this is what Hermes
keys off:

| code | meaning | what Hermes does |
|---|---|---|
| `0` | nothing new, stay quiet | nothing |
| `10` | positive signal | forward **stdout verbatim** to Telegram |
| `1` | error | report an error, at most once a day |

Exit `1` means the script itself crashed, or **every** source failed.  One
dead source is not an error: it becomes a `⚠ source: reason` line on stderr
and the rest of the run carries on (PLAN §6).

`status`, `list`, `add` and `del` exit `0` on success and `1` on a usage
error.  With no trigger firing, `check` still prints the snapshot (handy by
hand, ignored by Hermes) and exits `0`.  Re-running `check` on the same day
is idempotent: same text, same exit code, no duplicate rows.

## Configuration

| env var | default | meaning |
|---|---|---|
| `MUSHROOM_DB` | `./state.sqlite` | SQLite state (history, params cache, notifications) |
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

Fully offline: `tests/fixtures/` holds real responses recorded on
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
