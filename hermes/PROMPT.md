# Hermes: production contract

## Runtime

- `mushroom_brief.sh` runs before the agent. Its stdout is the complete input
  for the message; the agent must not run the application a second time.
- `daily_prompt.txt.in` is the daily 08:30 Europe/Prague template. It may return
  exactly `[SILENT]`.
- `friday_prompt.txt.in` is the Friday 19:00 template and always produces the
  weekend report.
- The application calculates the biological verdict, cycle phase and the
  per-day forecast verdict. Hermes only shortens and formats them. It must not
  promote a verdict from API30 or map values.
- `delivery_outcome` in the Hermes run journal is the transport result.
  Приложение не синхронизирует его обратно в SQLite.
- Exactly-once не гарантируется. If delivery fails after the notepad update,
  the durable state already contains the new verdict.

## Durable notepad binding

`cron notepad` accepts a cron job ID, not the human-readable job name. IDs and
the absolute Hermes CLI path are deployment data and are no longer committed
in prompt source. Render both prompts after resolving the actual job IDs on the
target host:

```bash
python3 hermes/render_prompts.py \
  --daily-job-id "$MUSHROOM_DAILY_JOB_ID" \
  --weekend-job-id "$MUSHROOM_WEEKEND_JOB_ID" \
  --hermes-cli /absolute/path/to/hermes \
  --output-dir /tmp/mushroom-prompts
```

Deploy the two rendered `.txt` files, never the `.txt.in` templates. The
renderer rejects relative CLI paths and malformed job IDs. The daily and
weekend jobs must never read or write each other's notepad.

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
procedure using the rendered files. Creating new jobs is deliberately not
documented here because their IDs must first be resolved and bound at deploy
time.
