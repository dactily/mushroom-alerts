# Main-only production releases

Production must run a reviewed commit from `main`. A successful deployment is
not complete until its PR is merged, the production checkout uses `main`, and
the deployed SHA and verification results are recorded. Use a merge commit
when reconciling an already deployed feature branch so its history remains
an ancestor of `main`.

## Required deployment gate

1. Review and merge the PR after CI passes. Never deploy an unmerged branch.
2. Fetch `origin/main` on the server. Obtain its full current SHA independently
   from GitHub or `git ls-remote origin refs/heads/main`. Stop if either lookup
   fails. Do not use a cached local SHA as the expected remote value.
3. Run the gate in the production checkout, using that independently obtained
   SHA:

   ```sh
   .venv/bin/python tools/check_release.py --expected-main FULL_REMOTE_MAIN_SHA
   ```

   This is read-only. It rejects tracked changes, detached HEAD, another
   branch, stale refs, and a deployed commit that is absent from `main`.
   Generated maps are allowed and are never deleted by the gate. Review any
   other untracked files before updating; Git must not overwrite them.
4. Preserve a Git bundle, the prior SHA, changed wrappers and job settings.
   Back up SQLite with its Backup API and check the copy's integrity.
5. Follow the application deployment procedure: preserve the enabled state of
   the two jobs, pause them for runtime changes, fast-forward to the exact
   checked SHA with `git merge --ff-only FULL_REMOTE_MAIN_SHA`, and verify on
   isolated database copies. Never use `reset --hard` on production. Resume
   only jobs that were previously enabled. Do not test by delivering a job.
6. Run the gate again with `--audit` and the same SHA. Verify wrappers, prompts,
   schedules, job states and the application health export separately.

The gate does not fetch, deploy, check CI, pause jobs, migrate SQLite or test
application health. It must be invoked by the operator before each deployment;
it cannot prevent an operator from bypassing the procedure with raw Git commands.
Keep rollback commits and snapshots. A deliberate rollback requires separate
authorization and verification; the normal gate rejects lost history.

## Periodic audit

Fetch fresh refs and obtain the remote main SHA, then run the gate with
`--audit`. Investigate a feature deployment, local modifications, a production
SHA absent from main, or pending main changes. Check for draft/open PRs that
contain the deployed commit and stale worktree registrations. Do not merge,
deploy, delete branches or restore data automatically during an audit.

Audit failures must remain visible, including unavailable SSH or GitHub access.
An audit requires a running scheduler and credentials; a written procedure is
not a background monitor. Check branch cleanup against `origin/main`, preserve
unmerged commits in a verified bundle, and use `git worktree prune --dry-run`
before pruning stale metadata.

## Reconciliation, 2026-11-17

Production was on `fix/upward-only-mushroom-alerts` at
`88a1e9308e14df9e2078fb019dc2146809810c99`. PR #8 had remained a draft since
2026-10-28; main was still `b905bf2f0dd16d63bb7ff2121d0fd58dfd4c5d54`.
The two feature commits implement and document the upward-only alert rule.

Before reconciliation, a verified complete source bundle, a SQLite Backup API
snapshot with integrity result `ok`, and a manifest of the deployed SHA, tree
and wrapper hashes were saved in the private server directory
`/home/ihor.travkin/releases/mushroom-alerts/main-reconciliation-20261117T011222Z/`.
The production tracked files were clean; generated `maps/` were retained.

Before local branch cleanup, all refs were saved in the verified ignored bundle
`.cache/git-recovery/before-cleanup-20261117.bundle`. This includes the two
unmerged commits from `exp/other-regions`. To recover that experiment:

```sh
git fetch .cache/git-recovery/before-cleanup-20261117.bundle \
  refs/heads/exp/other-regions:refs/heads/exp/other-regions
```

The bundle is local recovery material, not an off-host backup.
