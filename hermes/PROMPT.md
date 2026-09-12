# Hermes: production contract

## Runtime

- The wrapper script runs before the agent. Its stdout is the complete input
  for the message; the agent must not run the application a second time.
- `mushroom_brief.sh` (daily 08:30 Europe/Prague) runs
  `brief --mode daily`; `mushroom_weekend.sh` (Friday 19:00) runs
  `brief --mode weekend`.
- The application computes everything: per-location verdict, cycle phase,
  outlook, the caveats, and the send/silent decision. The block starts with
  `ОТПРАВЛЯТЬ: да|нет`. `нет` means the agent answers exactly `[SILENT]`.
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
2. a location verdict differs from the last daily report;
3. the candidate high-probability date newly entered the next 7 days,
   disappeared from them, or moved by more than 2 days;
4. `error_class` changed (`none` / `station` / `brief`); a dead ČHMÚ map or
   HoubyMapa never changes it and only appears in `ОГОВОРКИ`.

Weekend: always `да`. Re-running a mode on the same day is idempotent — the
comparison is always against the last report from an earlier date, and the
row for today is replaced.

Deterministic fallback without any agent reasoning is unchanged:
`python -m mushroom_alerts check`, exit `10` → forward stdout, `0` → silence,
`1` → error.

## Biological verdict v3

A rain episode requires at least 20 mm over three calendar days and complete
temperature coverage with a mean of 12–22 °C. Overlapping qualifying windows
are merged into one wet spell; separated spells remain independent. An older
active episode outranks a newer episode that is still waiting.

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
describe a window or decide how confident to sound.

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
