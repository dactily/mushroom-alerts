# Daily alert policy deployment, 2026-10-28

The daily report now sends only when at least one location increases by at
least 10 percentage points compared with its previous daily calculation and
the new chance is strictly greater than 60%. Threshold crossings below that
increase, decreases, first runs, new locations, missing values and source
status changes stay silent. Silent reports remain the comparison baseline.
The compact change list uses the same qualifying filter.

The Friday weekend report always sends. Daily collection remains at 08:30
and the Friday report remains at 19:00, Europe/Prague. Chance calculation,
rain and sustained-moisture windows, maps, dependencies and the SQLite schema
are unchanged. The rule version is 8.

## Deployment evidence

- Application code commit: `a67490e0b9b689004ecbfb1d6129fbce5d0ae562`.
- Working branch: `fix/upward-only-mushroom-alerts`; main was not merged.
- Local offline tests: 682 passed. GitHub Actions checks passed.
- Production Python offline tests: 682 passed; Ruff 0.16.7 passed.
- Both Mushroom jobs were paused during deployment, then their original
  enabled state was restored. Schedules, prompts, scripts and delivery
  settings were verified unchanged.
- Separate SQLite copies were used for daily and weekend CLI smoke runs
  with all 13 configured locations. Both returned valid JSON and exit 0.
- The daily smoke run returned `send=false` and no map attachment.
- The weekend smoke run returned `send=true`, compact text, and a
  1200 by 2350 pixel map attachment.
- The complete production `reports` table had the same digest before and
  after verification. Production report history was not rewritten.
- No Telegram test message or delivering cron job was invoked.

Protected deployment records are on the server under
`/home/ihor.travkin/releases/mushroom-alerts/upward-alerts-20261028-r1/`.
They contain the deployment manifest, before-job settings, wrapper copies,
test output, and a SQLite Backup API snapshot whose integrity check returned
`ok`. Keep these records private; job settings include delivery information.

## Code rollback

The previous production branch was `main`, at
`b905bf2f0dd16d63bb7ff2121d0fd58dfd4c5d54`. For an authorized rollback, pause
only the two Mushroom jobs, verify the checkout has no unrelated tracked
changes, switch back to that revision, verify the old daily/weekend behavior
on database copies, and restore the jobs' prior enabled state.

No database restore or schema rollback is required. Preserve current
observations and report history. A database restore is a separate operation
and would discard data collected after its snapshot.
