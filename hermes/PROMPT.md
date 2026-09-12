# Hermes: production contract

## Runtime

- `mushroom_brief.sh` runs before the agent. Its stdout is the complete input
  for the message; the agent must not run the application a second time.
- `daily_prompt.txt` is the daily 08:30 Europe/Prague contract. It may return
  exactly `[SILENT]`.
- `friday_prompt.txt` is the Friday 19:00 contract and always produces the
  weekend report.
- The application calculates the biological verdict, cycle phase and the
  per-day forecast verdict. Hermes only shortens and formats them. It must not
  promote a verdict from API30 or map values.
- `delivery_outcome` in the Hermes run journal is the transport result.
  Приложение не синхронизирует его обратно в SQLite.
- Exactly-once не гарантируется. If delivery fails after the notepad update,
  the durable state already contains the new verdict.

## Durable notepad binding

`cron notepad` accepts a cron job ID, not the human-readable job name. The
current production bindings are:

| Contract | Job name | Job ID |
|---|---|---|
| daily | `mushroom-daily` | `f28cb85d2a56` |
| weekend | `mushroom-weekend` | `17167162fb31` |

The literal IDs are intentionally present in the two prompt files. If a job
is recreated, replace its ID in the corresponding prompt before deployment.
The daily and weekend jobs must never read or write each other's notepad.

The CLI path in production is absolute:

```text
/home/ihor.travkin/.hermes/hermes-agent/venv/bin/hermes
```

This avoids Hermes resolving `~` relative to a profile workspace.

## Biological verdict v2

A rain episode requires at least 20 mm over three calendar days and complete
temperature coverage with a mean of 12–22 °C. It opens a calculated growth
window D+7...D+12, anchored on the wettest day.

Before D+7 the current verdict cannot be high, even if API30 already exceeds
25 mm or HoubyMapa is high. High is allowed only inside the calculated window
and additionally requires:

- fresh API30 at or above 25 mm;
- fresh forecast and a valid temperature gate;
- sufficient precipitation history and no frost in the available seven-day
  minimum-temperature history;
- fresh high support from at least one of ČHMÚ map or HoubyMapa.

The maps remain model evidence, not proof that mushrooms are present. Inside
the calculated window Hermes must describe growth as possible, not as a
confirmed wave.

## Deployment note

Copy the wrapper to the family profile and keep executable permissions:

```bash
cp hermes/mushroom_brief.sh ~/.hermes/profiles/family/scripts/
chmod 700 ~/.hermes/profiles/family/scripts/mushroom_brief.sh
```

Prompt updates are applied to the existing jobs by the server deployment
procedure. Creating new jobs is deliberately not documented here because the
new IDs must first be bound into the corresponding prompt files.
