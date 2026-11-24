# agent-harness

Small tools that let one person direct many coding-agent sessions (Claude Code) with human gates.

I run dozens of sessions a day: terminal tabs on a laptop (Herdr, a terminal multiplexer for agents) and tmux
sessions on a server. Left alone, they step on each other, wait on things nobody tracks, and touch production
without a clear yes. These scripts add ownership, waiting, and approval around the sessions. I call the result a
software factory: many workers, one human who decides.

Status: extracted from a personal setup. It works for me. Expect to adapt it: paths, hosts, and some message texts
are mine. Some card texts are still partly in Spanish (see "Known rough edges").

## The loop

```
  coding sessions (laptop tabs, server tmux)
        |  hooks register each session, its PRs, its chat threads
        v
  agent-bus  -- claim / park (typed waits) / handoff / initiatives -->  SQLite bus.db
        |                                            ^
        | needs a human                              | wait met -> resume the session
        v                                            |
  approval app (Polar)  -- owner taps Go / edit / no --> polar-notify  (types the answer into the session)
        |
        v
  release desk (one session)  -- one Go per batch -->  staging -> main
        |
        v
  prod-op  -- pin, ask, run exactly once after Go -->  production step
```

Polar is the separate Slack approval app these tools talk to (`polar ask ...`, `polar status`, `polar list`).
Any approval app with the same few commands can replace it.

## Tools

All are single-file Python 3 scripts with no third-party packages. Python 3.9 or newer.

### agent-bus
A SQLite bus of who owns what. Sessions claim a topic, a PR or a chat thread. A session can park with typed
waits and resume when the first one is met. A long session can hand off to a fresh one with a note.

```
agent-bus claim topic "billing export"        # own work that has no PR or thread yet
agent-bus claim pr my-repo#42
agent-bus owners pr my-repo#42                # JSON list, primary first
agent-bus park --wait person:Alex --wait time:+3h --wait ci:my-repo#42 \
               --about "reply on tariff" --note-file note.md
agent-bus handoff --note-file - --about "continue the import"
agent-bus initiatives                         # the owner's top-level goals
agent-bus hook < hook.json                    # Claude Code hook entry point
```
Wait kinds: `person`, `thread`, `time`, `prod`, `ci`, `polar`. Read `agent-bus --help` for everything.
Roles: `local` (default, one machine), or `server` + `client` (the bus lives on a server and the laptop reaches it
over ssh). Set `AGENT_BUS_ROLE` or `~/.config/agent-bus/role`.

### prod-op
A production step is pinned (a copy of its script folder plus file hashes), sent as an approval card, and run
exactly once after the owner chooses Go. The card title carries the hash. Changing the pinned files after the card
voids it. Only one kind ships: `script`. Add your own (a flag write, a dashboard publish) as a plugin.

Plugins: set `PROD_OP_PLUGINS=<dir>`. prod-op imports each `<dir>/*.py` (name order) and calls `register(op)`;
`op` is the prod-op module. A plugin adds one kind:

```python
def build(args, op_dir):          # -> (spec, summary, dry_text, detail); raise op.Refuse("text") to refuse
    return {"kind": "toy", "word": args.word}, "toy " + args.word, "dry: ...\n", "Toy says " + args.word

def apply(spec, op_dir, polar_id):  # runs only after Go -> (exit_code, output_text)
    return 0, "done\n"

def register(op):
    op.register_kind("toy", build, apply, lambda parser: parser.add_argument("--word", required=True))
```

`--why`, `--requester` and `--requester-label` exist for every kind. `op` also gives `Refuse`, `run_cmd`, `canon`,
`tail` and `NAME_RE`. A kind name that exists already is an error. Keep a plugin's settings (project refs, token
paths) in your own config file, not in this repo.

```
prod-op request script --name backfill-orders --why "fill 120 missing rows"   # needs $PROD_OP_SCRIPT_ROOT/backfill-orders/run.sh dry|apply
prod-op run <id>          # only after Go; refuses a second run
prod-op show <id> | list
```

### polar-notify
No model, runs every few seconds from cron/launchd/systemd. It reads decided approval cards and types the owner's
choice into the session that asked (through `agent-bus deliver`). A parked session is resumed instead.

### herdr-deliver
Types a message into a Claude Code pane or tmux session and makes sure it is submitted, without clobbering text
the owner is typing. `agent-bus` uses it.

### Release desk (docs/release-desk.md)
Not code: a contract for one long-lived agent session. It owns every `staging` to `main` promotion. Feature
sessions only merge into `staging` and leave `release-desk:` comments on their PR.

### examples/CLAUDE.md
The operating rules my agents follow: when to ask, how to claim and park, which model reads, how to report, how
chat goes in through a router and out through the approval app.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `AGENT_BUS_ROLE` | `local`, `server` or `client` | `local` |
| `AGENT_BUS_DB` | path of bus.db | `~/.local/state/agent-bus/bus.db` |
| `AGENT_BUS_SERVER_HOST` | ssh host of the server (client role) | `server` |
| `AGENT_BUS_SERVER_TMUX` | tmux binary on the server | `tmux` |
| `AGENT_BUS_GH_ORG` | GitHub org or user whose PRs the hook tracks | `your-org` |
| `AGENT_BUS_OWNER_NAMES` | comma list of the owner's chat names or ids (not treated as "another person") | `owner` |
| `AGENT_TASK_MODEL` | model for sessions the bus starts | `opus` |
| `POLAR_BIN` | approval app CLI (agent-bus, polar-notify and prod-op) | `polar` |
| `PROD_OP_SCRIPT_ROOT` | folder of prod-op script folders | `~/.local/share/agent-tasks` |
| `PROD_OP_PLUGINS` | folder of prod-op plugins (`*.py`, see prod-op) | none |
| `PROD_OP_SECRET_EXTRA` | one more regex that prod-op hides in output | none |
| `PROD_OP_TMUX` | tmux path for prod-op when it is not on PATH | `tmux` |
| `AGENT_BUS_LANG` | `en` or `es`: the fixed owner-facing texts (attach line, two-copies and taken-over notices, prod-op card) | `en` |
| `AGENT_BUS_MODEL_FULL_CMD` | command that exits 0 when the premium model is at its limit (then new sessions start on `opus`) | unset (never full) |
| `AGENT_BUS_MOVE_NOTE_TOOLS` | sentence about the CLI tools that need no browser, in the note of a session moved to the server | `CLI tools need no browser here.` |
| `POLAR_NOTIFY_SKIP_EXTRA` | comma list of extra requester prefixes polar-notify skips | none |

## Not included, and why

- The approval app (Polar) itself: separate project.
- The Slack router, the task coordinator, the release-desk gate script and the dashboards: tied to my chat
  workspace and my machines.
- Company skills, ticketing and database integrations, secret wrappers, account switching: specific to my employer
  and they touch credentials.
- Feature-flag, Grafana and SQL kinds of prod-op: they are plugins in my private setup. Only `script` is generic.

## Requirements

Python 3.9+, `git`, `gh` (for CI waits). Optional: tmux (server sessions), Herdr (laptop tabs), ssh (client role),
an approval app with a `polar`-like CLI.

## Tests

```
python3 -m venv .venv && .venv/bin/python -m unittest discover -s tests
```
496 tests (agent-bus, polar-notify, herdr-deliver) pass on my machine. They use a temp database and fakes. They
never reach a server.

## Known rough edges

- Some owner-facing card texts and the Polar status words in `polar-notify` are in Spanish. Translate them.
- The hooks installer is not included. Register `agent-bus hook` for Claude Code hook events yourself.
- It assumes macOS or Linux, and a single owner.

## License

MIT, Copyright (c) 2026 Cris Orona.
