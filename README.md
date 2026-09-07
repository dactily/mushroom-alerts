# mushroom-alerts

Daily mushroom-growth signal for a handful of places around Valašské
Meziříčí, assembled from ČHMÚ and HoubyMapa (and, later, ČHMÚ station open
data + Open-Meteo).  No LLM, no bot of its own: it is a CLI that Hermes runs
from cron and whose stdout it forwards to Telegram.  See `PLAN.md` for the
whole design, and `mushroom_alerts/base.py` for the contract every source
module follows.

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
🍄 Valašské Meziříčí: ČHMÚ 3/5, HoubyMapa 3/5 score 0.47
🍄 Valašská Bystřice: ČHMÚ 3/5, HoubyMapa 4/5 score 0.63
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
error.  Until `rules.py` exists, `check` prints the snapshot and exits `0`.

## Configuration

| env var | default | meaning |
|---|---|---|
| `MUSHROOM_DB` | `./state.sqlite` | SQLite state (history, params cache, notifications) |
| `MUSHROOM_LOCATIONS` | `./locations.yaml` | watched locations |
| `MUSHROOM_RECORD` | unset | `1` dumps every HTTP response into `tests/fixtures/` |

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

Both are undocumented endpoints and may change without notice; failures are
soft by design.
