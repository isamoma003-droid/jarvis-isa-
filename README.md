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
                    └──────────────┘
                           │
                           └──────────► Scout · Relay · Flux sub-agents
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
`/new`, `/approve <auto|prompt|deny>`, `/exit`. Ctrl-C stops the current turn;
Ctrl-D exits.

### The HUD

`jarvis web` serves a J.A.R.V.I.S. interface, not a chat box:

- an **arc reactor** that shifts colour and tempo with what Jarvis is doing —
  cyan idle, faster when thinking, amber while a tool runs, red on a fault,
  dimmed when the socket drops;
- a **telemetry rail** — session clock, tokens in and out, cached tokens,
  tools run, current state;
- **gold authorisation dialogs** for anything that needs your approval, with
  the exact command shown before you allow it;
- concentric rings and a slow radar sweep behind the conversation, scanlines
  over it, and a short boot sequence on load.

Readable first: the chrome glows, the body text doesn't — a HUD you can't
read a stack trace in is a failed HUD. It respects `prefers-reduced-motion`
(every animation here is decoration, never a barrier) and collapses to one
column on a phone.

## What it can do

| Group | Tools |
|---|---|
| Files | `read_file`, `write_file`, `edit_file`, `list_dir`, `find_files`, `search_text` |
| Shell | `run_shell` |
| Web | `web_search`, `web_fetch` (server-side, dynamic filtering) |
| Memory | `remember`, `recall`, `forget` |
| Reminders | `set_reminder`, `list_reminders`, `cancel_reminder` |
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

Everything durable lives in MongoDB — one database (`jarvis` by default) with
four collections:

| Collection | Holds |
|---|---|
| `facts` | what Jarvis knows about you, keyed and upserted |
| `sessions` | one document per conversation |
| `messages` | the transcripts |
| `reminders` | pending, done, and cancelled, with recurrence |

Facts are injected into the system prompt at session start, so Jarvis opens
already knowing what you told it last week — and because the store is a
server, the terminal, the web UI, and your phone all see the same memory.

Reminders get small integer ids from a `counters` document rather than
ObjectIds, so "cancel reminder 3" is a thing you can say out loud. A background
thread fires them into whichever interface is running — printed in the
terminal, pushed over the WebSocket, spoken aloud in voice mode. One-shot or
recurring (`daily`, `weekdays`, `every 30 minutes`).

Times are stored as native BSON dates in UTC so range queries and indexes work
properly, and handed back to callers as ISO strings. One `MongoClient` is
shared process-wide — pymongo pools internally, and a client per WebSocket
connection would exhaust a small Atlas tier.

## Safety

- **File and shell tools cannot leave the workspace.** Every model-supplied
  path is resolved and checked against the workspace root; `..`, absolute
  paths, and symlink escapes are refused. Default workspace is the current
  directory (`JARVIS_WORKSPACE` to change).
- **Approval gate.** Writes, edits, and state-changing shell commands ask
  first. `JARVIS_APPROVAL=auto` to stop asking, `deny` to forbid outright.
  Read-only commands (`ls`, `git status`, `cat`, …) never prompt.
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
effort = "high"           # low | medium | high | xhigh | max
approval = "prompt"       # auto | prompt | deny
workspace = "~/projects"
mongodb_db = "jarvis"
max_depth = 2
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
├── session.py     conversation state, persistence, reminder wiring
├── events.py      the event stream interfaces consume
├── config.py      defaults → config.toml → environment → flags
├── prompts.py     system prompts, split so the cached prefix stays stable
├── memory.py      MongoDB: facts, transcripts, reminders
├── reminders.py   "tomorrow at 9am" → a datetime, and the scheduler thread
├── tasks.py       background sub-agent tasks
├── toolkit.py     which tools an agent gets
├── tools/         files, shell, memory, reminders, web, delegation
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
can reach a real server even by accident. 109 tests, about three seconds.

## License

MIT
