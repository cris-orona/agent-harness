# Operating rules for coding agents (example)

This is a scrubbed copy of the rules my agents follow. Put it at `~/.claude/CLAUDE.md` (or the equivalent
for your agent) and adapt it. "The owner" is the one human who directs the sessions.

Write in short, active sentences. Use one term for one concept. Keep one topic in each paragraph.

## Workflow

- Start independent feature work in an isolated Git worktree. Prefer `staging` as the base when it exists. Use one
  branch per task and keep unrelated local changes.
- Do not search across the whole home folder. Search inside one repo or folder.
- Write large query, log or API output to a scratch file. Read only a summary or the lines you need.
- Use `gh` for GitHub.

## Asking the owner

Ask only when you need a fact, a decision, money, an outward message, a production write or a credential. Do not
ask for a go on a step the owner already asked for.

- A next step in the same task, a fix, a test, a draft PR, a review or a doc: do it. Report after.
- If you have a default and the step is easy to undo, state it in one line and continue. Do not ask.
- Do not end a reply with "Should I...?" for work inside the task scope.
- If you must ask, put each question with your default and the cost of the default in one short block. Ask several
  decisions in one message, not one by one.
- Before you ask for a fact, search memory notes and the repo. Do not ask for a fact you can read.

## Shell commands

- Do not start a Bash command with `cd`. Use `git -C <dir>`, `--repo`, `--workdir` or absolute paths.
- If a call is refused with "classifier unavailable", retry the same call once.

## Agent bus (session ownership)

`agent-bus` records which session owns which topic, PR or chat thread. Hooks register each session and the PRs and
threads it opens.

- Run `agent-bus claim topic "<short topic>"` when you own work that has no PR or thread yet.
- When you take over a PR or thread, run `agent-bus claim pr <repo>#<n>` or `agent-bus claim thread <channel>:<ts>`.
- A chat relay that is not yours: run `agent-bus notmine <kind> <key>` and say so. The router re-routes it.
- A relay that says "Also sent to ..." means two or more sessions own the key. Coordinate before you act: one
  session takes it as primary and tells the other.
- Use `agent-bus owners <kind> <key>` and `agent-bus who` to check ownership.

## Parking

Park a session only when the one thing left is outside your control and the wait is likely longer than one hour.
An outside wait is a person's reply, a thread reply, a time, a PR that reaches production, or CI.

1. Finish every open point you can do alone first. Do not park to avoid work.
2. If the wait is short (under about one hour), stay open and wait. An approval card you just sent is a short wait.
3. Commit and push your work in progress. `agent-bus park` refuses when the worktree has uncommitted changes.
4. Write a handoff note: the state now, the next step, and what the trigger should contain.
5. Declare every wait, then park:
   `agent-bus park --wait person:<name> --wait thread:<channel>:<thread_ts> --wait time:+3h --wait prod:<repo>#<n> --wait ci:<repo>#<n> --about "<one line>" --note-file -`
   The first wait that is met resumes you.
6. End your turn. The command closes your terminal tab after 15 seconds.
7. A finished session with no open ask does not park. It closes.

When agent-bus resumes you, a message says which wait was met. Read that message, then your handoff note
(`agent-bus note`), then continue the task.

## Token use: reading goes to a cheaper model

Each call re-reads your whole context, so long reading in the main session costs the most.

- Send reading-heavy work to a subagent on a cheaper model: repo exploration, log or transcript scans, long docs,
  wide read queries, "find where X happens". Ask for a short answer with file:line or row counts, not file dumps.
- Use the strongest model only when the subtask needs deep reasoning: a design choice, money or invariant logic, a
  hard bug. Say why in the prompt.
- Keep in the main session: decisions, edits to critical code and the final check.

## Report when you finish

When a task ends, or you stop for good, send one report through the approval app. The owner reads that app, not the
terminal.

- Body: the full structured report in the owner's language. Use sections and tables: what he asked, what you did, the
  numbers, what is in staging or production, what you checked and what you did not check, what is left and who has it.
- Add up to three short follow-ups he can start with one tap.
- Give documents as a link. Never give only a local path.
- For a visible change, attach a real screenshot.
- Skip the report for a park or a handoff: those have their own notes.

## Initiatives

An initiative is one top-level goal of the owner. Join the one your task belongs to
(`agent-bus initiatives`, `agent-bus initiative join <slug>`). When you finish, stop or park, write one note
(`agent-bus initiative note <slug> --text "<done. left. who has the next step>" --next "<next step>"`).
Do not create initiatives yourself. Unfinished work that no session will continue is a fact the owner must see:
write it in the note.

## Fresh restart (handoff)

A long context costs tokens on every turn. When your context is past about 200k tokens, restart fresh at your next
natural break: commit and push, write a handoff note, run `agent-bus handoff --note-file - --about "<one line>"`,
then end your turn. A new session starts from your note and keeps your claims, open waits and open cards.

## Promotions (release desk)

- A feature session does not promote `staging` to `main`. The release desk (one agent session, contract in
  `docs/release-desk.md`) owns every promotion.
- Your own PR: when the independent review is done, required checks are green and no review thread is open, merge it
  into `staging` at the reviewed head. Stop if the owner said "hold" or the PR has a `release-desk: hold` comment.
- After your PR merges into `staging`, it is in the queue. Tell the owner it is queued for the release desk.
- To steer the desk, comment on your PR: `release-desk: hold <reason>`, `release-desk: after <repo>#<pr>` or
  `release-desk: rollout <steps>`.
- Post-release steps of your PR (a flag on, a seed, a backfill) are yours to drive. Use `prod-op`:
  `prod-op request script --name <dir> --why "..."` pins the step and sends the approval card. After the owner's Go,
  run `prod-op run <id>` once. Do not make a production write any other way.

## Chat messages (router in, approval app out)

- Incoming: a router reads every DM and mention to the owner and relays each one to the session that owns its topic.
  Do not poll chat for replies yourself.
- Outgoing: never send a chat message to another person directly. Write the reply and put it in the approval app
  (`polar ask reply --channel <id> [--thread <ts>] --title ... --context ... --text-file <file>`). The owner says
  yes, edits or discards, and the app posts it as him. Write in the owner's voice and in the language of the thread.
- A yes/no or a choice: `polar ask decision ... --option key:Label` (2-4 options).
- Facts or short decisions the owner answers in words: `polar ask questions ...` (1-5 questions, with your default).
- A card that asks the owner to approve must explain the proposal in the card itself: what it is, what changes for
  whom, a real example and the risk. Never make him open the terminal to understand it.
- Do it yourself first. Before you ask a person to do something, check if you can do it. Show what can be seen:
  attach the real screenshot or file. Quote what you answer.
