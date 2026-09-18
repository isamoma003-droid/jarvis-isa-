# Jarvis — what this is and why

The plain-language spec. If you read one file before changing anything, read this
one. `README.md` documents how to *use* Jarvis; this documents what we decided and
what we deliberately did not build.

## Identity

**Name:** Jarvis.

**What it's for:** a personal assistant that runs on my machine, remembers me
between sessions, can act through tools I can see and stop, and can reach out to
me first when something is genuinely worth my attention.

**Who it's for:** just me. Memory is single-owner — there is no per-user
partitioning in the store, and adding one later means keying `facts` by owner, not
rewriting the harness.

**Tone:** warm but brief. Plain sentences, dry rather than eager. No preamble, no
restating the request, no summary of a summary. This lives in `prompts.py`
(`IDENTITY`) and should read the same in the terminal, the browser, and out loud.

## The first three capabilities

1. **Work on files and run commands** in a sandboxed workspace — read, edit,
   search, run the tests, report what actually happened.
2. **Answer questions using the web and my notes** — server-side search and fetch,
   with sources.
3. **Remember things and remind me** — durable facts across restarts, and
   reminders I can set in a sentence and cancel by number.

## Stack

| Piece | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Boring, good audio and HTTP libraries. |
| Brain | Claude Opus 5 via the official `anthropic` SDK | Behind a seam (`agent.py`) so the provider can change in one place. |
| Storage | MongoDB (Atlas free tier or local) | Memory follows me between the laptop, a VPS, and the phone. |
| Terminal | `rich` | |
| Web | FastAPI + WebSocket | |
| Interface design | the [Trillion](https://hellotrillion.ai) language | Near-black ground, one teal accent, a living orb, conversation floating over it. Amber is reserved for the confirmation gate so the one moment it asks me for something never looks like anything else. |
| Ears | **Deepgram** *(decided, not yet built)* | Streams; keeps the gap between releasing the key and being understood short. Local `faster-whisper` stays as the offline fallback. |
| Mouth | **ElevenLabs** *(decided, not yet built)* | Natural enough to feel like a presence. `pyttsx3`/`say` stay as the offline fallback. |

Runs laptop-first. The heartbeat (Tier 5) is deliberately separable so moving it
to an always-on host is a relocation, not a rewrite.

## How I talk to it

- **Typed**, in the terminal REPL or the browser. This path stays alive forever —
  it is how every future change gets debugged without talking to a computer.
- **Push-to-talk** — hold a key, speak, release. *Decided; Tier 3 work.* This
  becomes the default for `jarvis voice`.
- **Wake word** — the current open-mic loop, kept behind a flag for when I'm
  across the room.

## What it must never do without asking me

The hard gate. It sits between the model choosing a tool and the tool running, so
it covers typed, spoken, and heartbeat-initiated actions identically.

- **Anything that sends outward.** Email, messages, posts, pushes, any request
  that reaches another person or another machine. This one asks **every time**,
  even under `approval = "auto"` — an outward send is never pre-authorised by a
  policy setting or by a previous yes.
- **Writes, edits, and state-changing shell** — already gated by the approval
  policy since the first build; `auto` may waive these, `deny` refuses them.

Confirmation is **per action and does not generalise.** Approving one send does
not pre-authorise the next.

## Proactivity

Yes — but **quiet by default**. It earns the right to interrupt; it does not
assume it.

- Most checks produce nothing most of the time.
- Anything noteworthy lands in a calm inbox I can glance at and dismiss.
- Only `urgent` breaks through quiet hours.
- Nothing it notices while I'm away is dropped — notices are **held** and shown
  on my return.

## Tier status

| Tier | What it is | State |
|---|---|---|
| 1 | The brain — streaming text conversation loop | Done |
| 2 | The hands — tool registry, typed inputs, errors back to the model | Done |
| 3 | The ears and mouth — voice in, voice out | **Partial.** Wake word, local STT/TTS, speaks only after the turn ends. Push-to-talk, Deepgram, ElevenLabs, streaming speech and barge-in are the next tier. |
| 4 | The memory — durable facts across restarts | Done |
| 5 | The heartbeat — scheduled checks, held notices, quiet hours | Done |
| 6 | The rails — confirmation gate, untrusted content, audit log, kill switch | Done |

## Rules that outlast any one tier

- **One shared agent core, many ways in and out.** A typed turn, a spoken turn and
  a turn the heartbeat starts all flow through `Agent.run`. If the agent logic ever
  gets written twice, that's the bug.
- **The registry is the extension point.** A new capability is one self-contained
  tool registered in `toolkit.py` — never an edit to the core loop.
- **Everything it reads is data, not instructions.** A web page, a file, a command's
  output, a stored memory — none of it can tell Jarvis what to do. Instructions come
  from me, in conversation. Content that looks like an instruction gets surfaced to
  me, not obeyed.
- **Text before voice, always.** Voice is a thin adapter on a brain that already
  works in plain text.
- **Config over literals.** Thresholds, intervals, quiet hours, the model, which
  tools need confirmation — all live in `~/.jarvis/config.toml`. Tuning is a
  one-line edit, not a code change.
