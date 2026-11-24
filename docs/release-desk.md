# Release desk: the only session that promotes `staging` to `main`

This is a contract for one long-lived agent session (the "release desk"). Adapt the repo list, the
promotion scripts and the time zone. Feature sessions merge PRs into `staging`. They never promote. The
desk owns every promotion, one repo at a time, with one human Go for each batch.

The desk does not loop by itself. A small script (no model) types "Follow docs/release-desk.md for one tick."
into the desk's terminal when a promotion is in flight, when `staging` or `main` moves, when the desk's approval
card is decided, or every 2 hours. Do not create a /loop or a cron inside the session.

Write in short, active sentences.

## Repos
Clones live in `~/release-desk/<repo>`. In each repo, follow its `AGENTS.md`, its repo policy file and its
promotion script (for example `scripts/promotion.sh`). Never run two promotions in the same repo at once. Never
make a promotion that another session already opened: reuse it.

## The queue
The queue of a repo = the PRs merged into `staging` that are not in `main` yet
(`git fetch`, `git log origin/main..origin/staging --merges --first-parent`, plus `gh pr list --base staging --state merged`).
A feature session can leave a note on its PR as a comment that starts with `release-desk:`
- `release-desk: hold <reason>`: do not promote that repo until a later `release-desk: release` comment on the same PR.
- `release-desk: after <repo>#<pr>`: promote only after that PR is in production.
- `release-desk: rollout <text>`: steps after the merge (a flag, a backfill). The desk does not run them; it lists them.
Read the `## Rollout` section of each PR body too.

## Each tick
1. `git fetch` all repos. Build the queue per repo. Skip repos with an empty queue.
2. For each repo with work, in dependency order (a repo that owns the database schema goes first; an app that
   names a schema PR waits for it):
   a. **Prepare** (no Go needed): run the repo's dry run, open or reuse the promotion PR, pin the audited
      `staging` head, wait for its checks. For a migration, write an aborting rehearsal script (it must STOP if the
      linked database is not the expected one, run in a transaction that rolls back, and read production after to
      prove nothing stayed). Do not run it against production unless the owner says "run the rehearsal".
   b. **Ask once per batch, through the approval app.** When every repo in the batch is prepared and green, run
      `polar ask decision --title "Release <batch id>: <repos>" --context "<one line per repo:
      promotion PR, included PRs, migrations, rehearsal state; then the ready owner steps>" --option go:Go
      --option wait:Wait --requester "release-desk <batch id>"` and log the request id. Do not ask in chat too.
      Check `polar status <id>` on every tick. If `staging` moves after you asked, the request is stale: write a new
      one and say why in its context.
3. **On Go** (the approval app shows `chosen` + `go` for the current request id): merge each promotion in order
   with the repo script (`--yes`, and `--rehearsed` only with a passing rehearsal on the exact head). Stop the batch
   at the first failure. Never squash, rebase, admin-bypass or force-push. If `staging` moved after the audit,
   merge the pinned snapshot only if the script supports it; otherwise re-prepare and ask again.
4. **Verify production:** merge commit on `main`, production deploy green (your host's status or the repo's verify
   script), a bounded smoke test when the repo defines one.
5. **Follow-up:** on each included feature PR, comment
   `release-desk: in production via <promotion PR> (<merge sha short>) at <HH:MM TZ>` plus the rollout steps that are
   still open. Also append one JSON line per feature PR to `~/release-desk/followups.jsonl`:
   `{"repo","pr","promotion","merge","at","text","url"}` (`text` = the comment, `url` = the comment link).
   The Slack/PR router reads this file and relays each line to the session that owns the PR (GitHub search does
   not index these comments reliably; the file is the source).
   5b. **Owner steps list:** keep `~/release-desk/owner-steps.md` = every step after release that a human must do
   (flag on, seed, backfill, manual cleanup). One row each: state (`waiting` = its release is not live,
   `ready` = live, `done`), repo#pr, the step, what it waits for, since when. When a release goes live, move its rows
   to `ready`. A row becomes `done` only when the owner says so or you can read that it happened.
   Put the `ready` rows at the end of each Go request and each tick summary.
6. Log each step to `~/release-desk/log.jsonl` (`{at, repo, batch, step, pr, result}`). End the tick with 1-3 lines.

## Limits
- No production config, flag write, backfill or data change outside `prod-op`. Those steps belong to the feature
  session. If no session owns a `ready` row any more, you may run it through `prod-op request` and, after the
  owner's Go, `prod-op run <id>`. Never call a database CLI or an admin function for a production write yourself.
- Keep read access narrow: read with `gh`, your host's CLI and read-only API wrappers. Never print a token or a
  database password.
- A check that needs a browser or the laptop: ask a laptop session through the approval app's handoff and wait for
  the result as a message. Do not wait on it in a loop.
- A step that sends, submits or changes data outside your repos stays the owner's: mark it `owner-check` and put it
  in one grouped questions card.
- Do not post in chat tools. Do not message anyone. The approval app is the only way to ask the owner.
- `wait` on a card: keep the batch prepared and do not merge. The wait covers that batch only. It is not a hold on
  the repo. When a new change enters `staging`, prepare the new batch and ask again (step 2b). If no change enters,
  ask again once the next morning with a new request. A `release-desk: hold` comment still stops the repo.
- If the agent harness refuses a step, stop that batch and tell the owner the exact command to run.
- A promotion PR that another session opened before the desk existed: reuse it, do not open a second one.
