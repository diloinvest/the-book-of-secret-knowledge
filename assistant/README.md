# Atlas

A personal AI assistant that runs on your own machine, with your own
credentials, and reaches everything you point it at: the shell, the whole
filesystem, the network, and any service you connect over MCP. Built on the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk).

The premise is simple: **you decide what is worth doing and what is risky, not
the tool.** Atlas does not second-guess you. Its only safety mechanism is an
approval gate that *asks* before certain actions — and you choose how much it
asks, down to nothing at all.

> One honest limit: Atlas is only as unrestricted as its tools. The Claude model
> underneath still declines genuinely harmful or illegal content on its own —
> that is built into the model, not into Atlas, and no setting here changes it.

## What it can do

- **Shell & files** — the built-in Claude Code tools: read, write, edit, run
  commands, search, anywhere on disk.
- **`http`** — an arbitrary HTTP client for any URL, method, header and body.
  Reaches private hosts, token-gated APIs and non-HTML endpoints that `WebFetch`
  can't. Authenticate with `{{secret:NAME}}` placeholders so credentials never
  enter the conversation.
- **`python`** — run Python in a subprocess for quick computation and scripting.
- **`remember` / `recall` / `forget`** — durable memory in a local SQLite file,
  loaded into context at the start of every session.
- **`secret_list` / `secret_get`** — credentials from `~/.atlas/secrets.toml`.
- **`notify` / `system_info`** — desktop notifications and host status.
- **Any MCP server** — Gmail, GitHub, Notion, a database, your own — added in
  `config.toml` and reachable like any other tool.
- **Skills, commands, `CLAUDE.md`, `.mcp.json`** — loaded from `~/.claude` and
  `./.claude`, exactly as Claude Code loads them.
- **Web search & fetch**, subagents, and everything else the SDK ships.

## Install

```bash
cd assistant
pip install -e .
atlas init          # creates ~/.atlas/{config.toml, secrets.toml, memory.db}
```

Authentication follows the Agent SDK: set `ANTHROPIC_API_KEY`, or use whatever
credential your environment already provides.

## Use

```bash
atlas                                   # interactive session
atlas ask "summarize today's git log"   # one-shot
cat error.log | atlas ask "what failed?"  # reads stdin when piped
atlas --gate open ask "..."             # no confirmations, this run
atlas run inbox                         # run one configured task now
atlas daemon                            # run all configured tasks on schedule
atlas memory                            # inspect stored facts
atlas memory --set key=value            # add one by hand
```

Inside a session: `/gate open|danger|strict`, `/model NAME`, `/memory [q]`,
`/audit [n]`, `/sessions`, `/mcp`, `/exit`.

## The gate — the only thing that ever stops a call

Three modes. **The gate never decides something is forbidden; it only decides
whether to ask you first.** Every "no" comes from you typing it.

| Mode     | Behaviour                                                            |
| -------- | ------------------------------------------------------------------- |
| `open`   | Nothing is ever asked. Full speed, no seatbelt.                     |
| `danger` | Asks only before destructive or outward-facing calls (default).    |
| `strict` | Asks before anything that is not plainly read-only.                |

When it asks, you answer: **y** (yes), **a** (yes, and every call to that tool
this session), **n** (no), or just type a sentence — that sentence goes back to
Claude as a course-correction. Set `gate = "open"` in your config to run with
zero prompts permanently.

What counts as "destructive or outward-facing" in `danger` mode is a list of
patterns in `config.toml` (`rm -rf`, `git push --force`, `DROP TABLE`, sending
mail, moving money, publishing, `sudo`, …). Extend it with `extra_danger_patterns`
or replace it entirely with `danger_patterns`.

Every tool call is written to an append-only audit log in `memory.db`, whatever
the gate decided. `atlas` in a session shows it with `/audit`.

The one thing no setting overrides: `rm`/`rmdir` targeting a critical system path
(`/`, `/home`, …). The SDK routes those to a confirmation even in `open` mode; in
an unattended run they are denied. That floor lives below Atlas.

## Configuration

Everything lives in `~/.atlas/config.toml`. See
[`config.example.toml`](config.example.toml) for the full annotated set. The
essentials:

```toml
model = "claude-opus-5"
gate = "danger"                 # open | danger | strict
workdir = "~/"
persona = "Answer in Bulgarian. Be direct."

[mcp_servers.github]
type = "http"
url = "https://api.githubcopilot.com/mcp/"
headers = { Authorization = "Bearer ghp_..." }

[tasks.inbox]                   # `atlas daemon` runs this on a schedule
every = "30m"
gate = "open"                   # unattended: a gate that would ask blocks
prompt = "Check my inbox; notify me about anything needing a reply today."
```

Credentials go in `~/.atlas/secrets.toml` (chmod 600) and are referenced as
`{{secret:NAME}}` from the `http` tool, so they stay out of the transcript.

## Layout

```
atlas/
  config.py    configuration + secrets loading
  memory.py    SQLite: durable facts + audit log
  tools.py     Atlas' own in-process tools (http, python, memory, ...)
  gate.py      the approval gate (PreToolUse hook + can_use_tool callback)
  session.py   assembles ClaudeAgentOptions
  cli.py       REPL, one-shot, scheduler
```

## How the gate is wired

The session runs in permission mode `default` with no allow-rules, and a
`PreToolUse` hook — which runs before every other permission step, for every
tool — is the single authority. For each call the gate classifies it: calls it
clears return `permissionDecision: "allow"`; calls it wants confirmed return
`"ask"`, which routes them to the `can_use_tool` callback where you answer. This
is why the gate, and only the gate, governs every tool — built-in and MCP alike.
