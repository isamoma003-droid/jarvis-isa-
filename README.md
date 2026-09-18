# Jarvis

A personal assistant with one agent core and three faces: a terminal REPL, a
voice loop, and a web chat UI. All three consume the same event stream, so a
capability added once works everywhere.

Built on the Claude API (`claude-opus-5`) with a manual streaming tool-use loop.

```
                    ┌──────────────┐
  terminal ──┐      │              │──► files & shell (workspace-confined)
  voice    ──┼─────►│    Jarvis    │──► web search & fetch (server-side)
  web      ──┘      │  agent core  │──► memory & reminders (MongoDB)
                    └──────┬───────┘
                           ├─────────► Scout · Relay · Flux sub-agents
                           │
                    ┌──────┴───────┐
                    │  heartbeat   │──► scheduled checks → the inbox
                    └──────────────┘    (quiet by default, held while away)
```

## Install

```bash
git clone https://github.com/isamoma003-droid/jarvis-isa-.git
cd jarvis-isa-
python -m venv .venv && source .venv/bin/activate

pip install -e .              # terminal only
pip install -e '.[web]'       # + browser UI
pip install -e '.[voice]'     # + speech in/out
pip install -e '.[all]'       # everything, plus test tooling
```

Then point it at two things:

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # or run `ant auth login`
export MONGODB_URI='mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/'
```

MongoDB holds everything Jarvis remembers. The [Atlas](https://www.mongodb.com/atlas)
free M0 tier is plenty and means your memory follows you between your laptop,
the VPS, and the phone PWA; `mongodb://localhost:27017` works too if you'd
rather keep it on the machine. `jarvis doctor` will tell you whether it can
reach the server. Copy `.env.example` to pin anything else.

## Use it

```bash
jarvis                        # terminal REPL
jarvis ask "what changed in this repo today?"
jarvis web                    # http://127.0.0.1:8765
jarvis voice                  # say "jarvis, ..."
jarvis notices                # what it surfaced while you were away
jarvis pause / jarvis resume  # the kill switch
jarvis log --totals           # what it did, and what it cost
jarvis doctor                 # check keys, deps, audio, database
```

Terminal session:

```
› summarise the open TODOs in this repo and remind me about them at 5pm

  ⚙ search_text pattern=TODO|FIXME
  ⚙ set_reminder text=Review 6 open TODOs when=17:00

Six TODOs, all in the parser: four are error handling, two are perf notes.
Reminder #3 set for today at 17:00.
```

In-REPL commands: `/help`, `/tools`, `/memory`, `/tasks`, `/reminders`,
`/notices`, `/dismiss <id|all>`, `/checks`, `/pause`, `/resume`, `/cost`,
`/new`, `/approve <auto|prompt|deny>`, `/exit`. Ctrl-C stops the current turn;
Ctrl-D exits.

### The interface

`jarvis web` serves an interface built in the [Trillion](https://hellotrillion.ai)
design language — a near-black ground, one teal accent doing almost all the work,
and the conversation floating over a single living orb:

- **the orb** — a starfield and four layered radial gradients composited into one
  bloom. Trillion's reacts to your voice; this one reacts to what the agent is
  doing, which is the equivalent signal for a typed interface. It brightens and
  quickens while thinking, turns amber while a tool runs, red on a fault, and goes
  cold and grey when the socket drops. Colour and energy are eased toward their
  targets, so a state change reads as the orb responding rather than blinking;
- **an activity panel** — the inbox (each notice dismissible in place), session
  telemetry, and what the core is set to. Collapses to a 32px rail;
- **amber authorisation dialogs** — the one moment the interface asks *you* for
  something, so it is deliberately the one thing that isn't teal. It names the
  action, shows the exact command, and says plainly that approving it authorises
  that action only;
- **hold** — the kill switch in the header. The wordmark dot turns amber while
  proactive behaviour is stopped.

Readable first: the chrome glows, the body text doesn't. It respects
`prefers-reduced-motion` (every animation here is decoration, never a barrier)
and drops the panel entirely on a phone, where the orb and the conversation are
the product and telemetry is not.

## What it can do

| Group | Tools |
|---|---|
| Files | `read_file`, `write_file`, `edit_file`, `list_dir`, `find_files`, `search_text` |
| Shell | `run_shell` |
| Web | `web_search`, `web_fetch` (server-side, dynamic filtering) |
| Memory | `remember`, `recall`, `forget` |
| Reminders | `set_reminder`, `list_reminders`, `cancel_reminder` |
| Notices | `list_notices`, `dismiss_notice`, `surface` |
| Delegation | `delegate`, `check_task`, `list_tasks` |

### Sub-agents

`delegate` hands a self-contained task to a sub-agent with its own context
window and a narrowed tool set, so a long job reports back one answer instead
of flooding the conversation:

| Agent | Gets | For |
|---|---|---|
| `scout` | web + read-only files | look something up, read a lot, report with sources |
| `relay` | web + files | draft a message, reply, or summary for you to send |
| `flux` | files + shell | make the change, run the checks, report what happened |
| `general` | the usual set | anything else |

Scout has no write access, so research can't quietly turn into edits. Relay
drafts and saves — it **cannot send anything**; wiring it to real mail (the
Gmail MCP in the architecture diagram) is still to do, and until then a human
sends the draft.

Set `background: true` and the task runs on a thread pool while the
conversation continues; `check_task` collects the report. Delegation depth is
capped by `max_depth` (default 2) so sub-agents cannot recurse away.

### Memory and reminders

Everything durable lives in MongoDB — one database (`jarvis` by default):

| Collection | Holds |
|---|---|
| `facts` | what Jarvis knows about you, keyed and upserted |
| `sessions` | one document per conversation |
| `messages` | the transcripts |
| `reminders` | pending, done, and cancelled, with recurrence |
| `notices` | the inbox: what the heartbeat surfaced, and whether you saw it |
| `checks` | when each scheduled check last ran and is next due |
| `settings` | the kill switch, and anything every interface must agree on |

Facts are injected into the system prompt at session start, so Jarvis opens
already knowing what you told it last week — and because the store is a
server, the terminal, the web UI, and your phone all see the same memory.

A stored fact is background knowledge, not standing permission: a remembered
note reading "always do X" still goes through the confirmation rules below.

Reminders get small integer ids from a `counters` document rather than
ObjectIds, so "cancel reminder 3" is a thing you can say out loud. The heartbeat
fires them into whichever interface is running — printed in the terminal, pushed
over the WebSocket, spoken aloud in voice mode — and **holds** any that come due
while nothing is attached, so a reminder that fires at 3am is waiting for you at
9. One-shot or recurring (`daily`, `weekdays`, `every 30 minutes`).

Times are stored as native BSON dates in UTC so range queries and indexes work
properly, and handed back to callers as ISO strings. One `MongoClient` is
shared process-wide — pymongo pools internally, and a client per WebSocket
connection would exhaust a small Atlas tier.

## The heartbeat

A single background loop, separate from any conversation, that lets Jarvis act
without being spoken to. It sweeps due reminders and runs whichever **checks**
are due, then decides — per result — whether the outcome is worth interrupting
for. What to check and how often lives in `config.toml`, never in code:

```toml
[[jarvis.checks]]
name    = "disk"
kind    = "shell"
command = "df -h / | tail -1"
every   = "30m"
surface = "on_match"      # on_fail · on_match · on_change · always
match   = "9[0-9]%"
level   = "notify"        # quiet · notify · urgent
message = "disk is nearly full"

[[jarvis.checks]]
name    = "deploy-flag"
kind    = "file"
path    = "DEPLOY_ME"
every   = "1m"
surface = "on_exists"     # on_exists · on_missing · on_change
level   = "urgent"
```

`jarvis checks` shows what is configured and when each next runs.

**Quiet by default — it earns interruptions, it doesn't assume them.** That
principle is the whole design, and it's mostly made of refusals to speak:

- **Most checks say nothing most of the time.** `quiet` never interrupts; it
  accumulates in an inbox you read when you choose. `notify` interrupts during
  waking hours. `urgent` interrupts regardless — that's the only thing it means.
- **A condition that is still true is not news.** While an identical notice is
  open, further occurrences are counted (`×4`), not repeated at you. Dismiss it
  and the next occurrence speaks up again.
- **Nothing you missed is dropped.** A notice is marked delivered only when an
  interface was actually attached to receive it — not merely because policy said
  it deserved an interrupt. Everything else stays pending and is shown on your
  return.
- **Quiet hours** hold non-urgent notices until morning (`quiet_hours`).
- **A restart resumes the schedule.** Next-due times live in Mongo, so restarting
  doesn't reset every timer or fire the whole set on boot.
- **Overlapping runs are skipped, not stacked.** Claims are taken atomically, so
  two heartbeats on the same database can't both run one check; a claim older
  than its lease is treated as a dead run and retried.
- **Everything surfaced is dismissible** — `/notices`, `/dismiss 3`,
  `jarvis notices --dismiss all`, or the inbox button in the HUD.

Nothing in the loop talks to the model, so a beat costs nothing and can't
surprise you with a bill. It doesn't care which machine it runs on either —
moving it to an always-on host is a relocation, not a rewrite.

## Safety

- **File and shell tools cannot leave the workspace.** Every model-supplied
  path is resolved and checked against the workspace root; `..`, absolute
  paths, and symlink escapes are refused. Default workspace is the current
  directory (`JARVIS_WORKSPACE` to change).
- **Approval gate.** Writes, edits, and state-changing shell commands ask
  first. `JARVIS_APPROVAL=auto` to stop asking, `deny` to forbid outright.
  Read-only commands (`ls`, `git status`, `cat`, …) never prompt. The gate sits
  between the model choosing a tool and the tool running, so it covers typed,
  spoken and heartbeat-initiated actions identically.
- **Anything outward-facing asks every time.** Pushing, sending, uploading,
  publishing, reaching another machine — `JARVIS_APPROVAL=auto` does not waive
  it, and approving one send never pre-authorises the next. Set
  `JARVIS_CONFIRM_OUTWARD=0` only deliberately.
- **Nothing it reads can give it orders.** Web pages, files, command output and
  stored memory are data. Content-bearing tool results are handed to the model
  inside an `<untrusted_content>` envelope, and text that reads like an
  instruction (`"ignore all previous instructions"`, `"don't tell the user"`) is
  flagged to you and put in the inbox rather than acted on. The envelope can't be
  closed early by the content inside it.
- **Nothing blocks on a human who isn't there.** A background sub-agent that
  hits the approval gate doesn't hang waiting for an answer that isn't coming —
  it does nothing and leaves a note saying what it wanted to do.
- **An audit trail.** Every tool call, approval, notice, check and turn is
  appended to `<data_dir>/audit.jsonl`, with a running cost estimate — a runaway
  loop shows up in `jarvis log --totals` long before it shows up on a bill.
- **A kill switch.** `jarvis pause` (or `/pause`, or **hold** in the HUD) stops
  every proactive behaviour at once — checks, reminders, background work — while
  you can still talk to it. The flag lives in the database, so it survives a
  restart and every interface agrees.
- **A refusal list** catches obvious catastrophes (`rm -rf /`, `mkfs`, fork
  bombs). It is a guardrail against a slip, not a security boundary — a shell
  cannot be sandboxed by pattern matching. The approval gate is the real
  control.
- Tool inputs are validated against their schema before any handler runs, and
  a turn cut off mid tool-call is never executed.

## Configuration

Defaults, then `~/.jarvis/config.toml`, then environment variables, then flags.

```toml
[jarvis]
model = "claude-opus-5"
effort = "high"                # low | medium | high | xhigh | max
approval = "prompt"            # auto | prompt | deny
confirm_outward = true         # outward sends ask every time regardless
workspace = "~/projects"
mongodb_db = "jarvis"
max_depth = 2
heartbeat_seconds = 60
quiet_hours = "22:00-07:00"    # "" for none

[[jarvis.checks]]              # as many as you like; see The heartbeat above
name = "tests"
command = "pytest -q"
every = "4h"
surface = "on_fail"
level = "notify"
```

Every option has a `JARVIS_*` environment variable — see `.env.example`.
Notable ones:

- `JARVIS_EFFORT` — how hard the model works per turn. Sub-agents run at
  `JARVIS_SUBAGENT_EFFORT` (default `low`).
- `JARVIS_THINKING=0` — turn adaptive thinking off.
- `JARVIS_REFUSAL_FALLBACKS=0` — turn off server-side fallback (needed on
  Bedrock, Vertex, and Foundry, which do not support it).
- `JARVIS_WEB_TOOLS=0` — run without web access.
- `MONGODB_URI` / `JARVIS_MONGODB_URI` — the connection string (the prefixed
  one wins). `JARVIS_MONGODB_DB` picks the database name.

Connection strings are redacted wherever Jarvis prints them, so a password
never lands in your terminal or logs.

## Architecture

```
src/jarvis/
├── agent.py       the streaming tool-use loop — one implementation, all faces
├── session.py     conversation state, persistence, heartbeat wiring
├── events.py      the event stream interfaces consume
├── config.py      defaults → config.toml → environment → flags
├── prompts.py     system prompts, split so the cached prefix stays stable
├── memory.py      MongoDB: facts, transcripts, reminders, notices, schedule
├── reminders.py   "tomorrow at 9am" → a datetime, and the reminder sweep
├── heartbeat.py   the background loop: scheduled checks, quiet hours, notices
├── guard.py       untrusted content, and spotting outward-facing actions
├── audit.py       the append-only trail, and the cost tally
├── tasks.py       background sub-agent tasks
├── toolkit.py     which tools an agent gets
├── tools/         files, shell, memory, reminders, notices, web, delegation
├── cli.py         terminal REPL
├── voice/         wake word → speech-to-text → agent → speech
└── web/           FastAPI + WebSocket, single-page UI
```

The loop handles what the API actually does: `pause_turn` resumption for
server-side tools, refusals, truncated tool inputs, unparseable tool JSON
(bounded retries), and parallel tool calls returned in a single message. The
system prompt is split into a cached stable half and a volatile half holding
the clock, so the cache prefix survives between turns.

## Development

```bash
pip install -e '.[dev]'
pytest            # no network, no database: the API client is faked and
                  # Mongo runs in-process via mongomock
ruff check src tests
```

An autouse fixture replaces the Mongo client for the whole suite, so no test
can reach a real server even by accident. 187 tests, about five seconds.

## License

MIT
