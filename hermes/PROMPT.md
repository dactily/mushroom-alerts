# Hermes: production contract

## Runtime

- The wrapper script runs before the agent. Its stdout is the complete input
  for the message; the agent must not run the application a second time.
- `mushroom_brief.sh` (daily 08:30 Europe/Prague) runs
  `brief --mode daily`; `mushroom_weekend.sh` (Friday 19:00) runs
  `brief --mode weekend`.
- The application computes everything: the per-location chance in percent,
  the cycle phase, the two technical lines, the caveats, and the send/silent
  decision. The block starts with `ОТПРАВЛЯТЬ: да|нет`. `нет` means the
  agent answers exactly `[SILENT]`.
- The list under `ШАНС` is in the order of `locations.yaml` and must stay
  that way: the user keeps his own order and decides himself where to
  drive. Neither the script nor the agent ranks or recommends locations.
- `daily_prompt.txt` and `friday_prompt.txt` are the two prompts. They only
  tell the agent to obey that flag and to re-word the block as a Telegram
  message, keeping every number and date verbatim.
- Continuity is no longer the agent's business: the durable notepad
  (`mushroom_state`) and `render_prompts.py` are gone. The previous report
  lives in the SQLite table `reports`, and the script compares against it.
  Nothing needs job IDs or the Hermes CLI path any more, so both prompts are
  deployed as-is.
- `delivery_outcome` in the Hermes run journal is the transport result.
  Приложение не синхронизирует его обратно в SQLite.
- Exactly-once не гарантируется. If delivery fails after the row in
  `reports` was written, the stored state already contains the new verdict.

## Send rules (in the script, not in the prompt)

Daily, `send = да` when at least one holds:

1. there is no daily report from an earlier date (first run);
2. the chance of any location moved by 10 points or more against the last
   daily report;
3. the best chance crossed 60 % in either direction;
4. a location appeared that the last report did not have;
5. a location gained or lost its number («нет данных» → a percentage, or a
   percentage → «нет данных»); «нет данных» on both sides is not news;
6. `error_class` changed (`none` / `station` / `brief`); a dead ČHMÚ map or
   HoubyMapa never changes it and only appears in `ОГОВОРКИ`.

With twenty locations the previous rule ("any verdict changed") fired
almost every day, because a verdict is a coarse word; a percentage moves
smoothly, so the rule is about the size of the move.

Weekend: always `да`. Re-running a mode on the same day is idempotent — the
comparison is always against the last report from an earlier date, and the
row for today is replaced.

Deterministic fallback without any agent reasoning is unchanged:
`python -m mushroom_alerts check`, exit `10` → forward stdout, `0` → silence,
`1` → error.

## Chance in percent v4

The block reports one comparable number per location instead of the
four-word verdict: on 12.09.2026 eight locations all came out «средняя»
while HoubyMapa ranged 0.62–0.90 and station API30 20–27 mm. The number is
a product of the rain-episode phase, the API30 band, the temperature gate,
frost, and — for today only — the two maps; it is rounded to 5 % inside
5–95 % and capped at 50 % when the station is missing or stale. It is not
calibrated against finds and is not a probability: it says how much the
site today looks like conditions under which mushrooms come.

A day whose API30 is absent or comes from a stale release gets **no number
at all**: the block prints «нет данных» for it, names it under `ОГОВОРКИ`
(«нет свежего API30»), and leaves it out of `ПОДРОБНО`. A missing
measurement used to enter as a neutral multiplier, which raised the result —
20 mm scored 45 % and no number at all scored 55 %. Today falls back to the
station's own API30 when only the forecast release went stale, so an old
release costs the forecast days and not the day that was measured.

## Biological verdict v3

A rain episode is a run of wet days (≥ 1 mm in a day) that reaches at least
20 mm over some three calendar days with complete temperature coverage and a
mean of 12–22 °C. One dry day may sit inside an episode; two dry days end it,
so two rains a week apart stay two episodes with two anchors instead of
collapsing into one anchored on the newer rain. A day with no measurement is
neither wet nor dry and never splits an episode. An older active episode
outranks a newer episode that is still waiting.

Before D+7 the current verdict cannot be high, even if API30 already exceeds
25 mm or HoubyMapa is high. High is allowed only in the D+7...D+12 primary
window. D+13...D+21 is a residual phase capped at medium. High additionally
requires:

- fresh API30 at or above 25 mm;
- fresh forecast and a valid temperature gate;
- sufficient precipitation history and no frost in the available seven-day
  minimum-temperature history;
- fresh high support from at least one of ČHMÚ map or HoubyMapa.

The maps remain model evidence, not proof that mushrooms are present. The
wording for every phase is produced by `report.py`, so the agent never has to
describe a window or decide how confident to sound. The verdict itself is
unchanged by the percentage; it stays in the debugging brief.

## Deployment note

Copy both wrappers to the family profile and keep executable permissions:

```bash
cp hermes/mushroom_brief.sh hermes/mushroom_weekend.sh \
   ~/.hermes/profiles/family/scripts/
chmod 700 ~/.hermes/profiles/family/scripts/mushroom_brief.sh \
          ~/.hermes/profiles/family/scripts/mushroom_weekend.sh
```

Prompt updates are applied to the existing jobs by the server deployment
procedure using `hermes/daily_prompt.txt` and `hermes/friday_prompt.txt`
verbatim. The two jobs are independent and share nothing but the SQLite file.
