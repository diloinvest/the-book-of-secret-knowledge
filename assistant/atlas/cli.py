"""Atlas command line: an interactive REPL, one-shot questions, and the
scheduler for unattended work.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import warnings
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    query,
)

from .config import Config, Task, load_config, parse_interval
from .gate import Gate
from .memory import Memory
from .session import build_options

console = Console()

# The SDK auto-adds the schema-less "Skill" loader to the approved set and warns
# that the callback won't see it. Loading a skill's instructions is not an action
# worth gating, so silence just that one warning.
try:
    from claude_agent_sdk.types import CanUseToolShadowedWarning
    warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)
except Exception:
    pass

GATE_HELP = {
    "open": "nothing is ever asked",
    "danger": "asks before destructive or outward-facing calls",
    "strict": "asks before anything that is not read-only",
}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _summarize_input(tool: str, data: dict[str, Any]) -> str:
    for key in ("command", "url", "file_path", "pattern", "path", "query", "prompt", "code", "key"):
        if key in data and isinstance(data[key], str):
            value = data[key].strip().replace("\n", " ⏎ ")
            return value if len(value) <= 160 else value[:157] + "..."
    rendered = json.dumps(data, ensure_ascii=False, default=str)
    return rendered if len(rendered) <= 160 else rendered[:157] + "..."


def render_stream_block(block: Any) -> None:
    if isinstance(block, TextBlock):
        if block.text.strip():
            console.print(Markdown(block.text))
    elif isinstance(block, ThinkingBlock):
        console.print(f"[dim italic]{block.thinking.strip()[:400]}[/]")
    elif isinstance(block, ToolUseBlock):
        console.print(f"  [cyan]▸ {block.name}[/] [dim]{_summarize_input(block.name, block.input or {})}[/]")


async def ask_approval(tool: str, data: dict[str, Any], reason: str) -> str:
    """The confirmation prompt. Only reached for calls the gate escalated."""
    body = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(body) > 2000:
        body = body[:2000] + "\n... [truncated]"
    console.print()
    console.print(
        Panel(
            Syntax(body, "json", theme="ansi_dark", word_wrap=True),
            title=f"[bold yellow]{tool}[/]",
            subtitle=f"[dim]{reason}[/]",
            border_style="yellow",
        )
    )
    console.print(
        "[bold]run it?[/] [green]y[/]=yes  [green]a[/]=yes, and every "
        f"{tool} this session  [red]n[/]=no  [dim]or type why not[/]"
    )
    answer = (await asyncio.to_thread(input, "> ")).strip()
    lowered = answer.lower()
    if lowered in {"y", "yes", "да", ""}:
        return "yes"
    if lowered in {"a", "always", "всички"}:
        return "always"
    if lowered in {"n", "no", "не"}:
        return "no"
    return answer


# --------------------------------------------------------------------------
# runtime
# --------------------------------------------------------------------------
class Atlas:
    def __init__(self, cfg: Config, interactive: bool = True):
        self.cfg = cfg
        self.memory = Memory(cfg.db_path)
        self.gate = Gate(cfg, self.memory, prompt=ask_approval if interactive else None)

    def options(self, **kwargs):
        return build_options(self.cfg, self.memory, self.gate, **kwargs)

    async def drain(self, stream, show_tools: bool = True) -> ResultMessage | None:
        """Print messages as they arrive; return the final result."""
        result = None
        async for message in stream:
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock) and not show_tools:
                        continue
                    render_stream_block(block)
            elif isinstance(message, SystemMessage) and message.subtype == "init":
                broken = [
                    s for s in (message.data.get("mcp_servers") or [])
                    if s.get("status") in ("failed", "needs-auth")
                ]
                if broken:
                    names = ", ".join(f"{s['name']} ({s['status']})" for s in broken)
                    console.print(f"[yellow]MCP servers unavailable:[/] {names}")
            elif isinstance(message, ResultMessage):
                result = message
        return result

    def close(self) -> None:
        self.memory.close()


def _print_result_footer(result: ResultMessage | None) -> None:
    if result is None:
        return
    bits = []
    cost = getattr(result, "total_cost_usd", None)
    if cost:
        bits.append(f"${cost:.4f}")
    duration = getattr(result, "duration_ms", None)
    if duration:
        bits.append(f"{duration / 1000:.1f}s")
    turns = getattr(result, "num_turns", None)
    if turns:
        bits.append(f"{turns} turns")
    if bits:
        console.print(f"[dim]{' · '.join(bits)}[/]")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
async def cmd_chat(cfg: Config, args) -> int:
    atlas = Atlas(cfg)
    options = atlas.options(resume=args.resume, continue_conversation=args.continue_last)

    console.print(
        Panel(
            f"[bold]Atlas[/] · {cfg.model} · gate [bold]{atlas.gate.mode}[/] "
            f"([dim]{GATE_HELP[atlas.gate.mode]}[/])\n"
            f"[dim]{cfg.workdir}[/]\n\n"
            "[dim]/gate open|danger|strict · /model NAME · /memory [q] · /audit [n]\n"
            "/sessions · /mcp · /new · /exit[/]",
            border_style="blue",
        )
    )

    try:
        async with ClaudeSDKClient(options=options) as client:
            while True:
                try:
                    line = (await asyncio.to_thread(console.input, "\n[bold blue]›[/] ")).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line:
                    continue
                if line.startswith("/"):
                    if await handle_command(line, atlas, client):
                        break
                    continue
                console.print()
                try:
                    await client.query(line)
                    result = await atlas.drain(client.receive_response())
                    _print_result_footer(result)
                except KeyboardInterrupt:
                    await client.interrupt()
                    console.print("[yellow]interrupted[/]")
    finally:
        atlas.close()
    console.print("[dim]bye[/]")
    return 0


async def handle_command(line: str, atlas: Atlas, client: ClaudeSDKClient) -> bool:
    """Handle a /command. Returns True when the REPL should exit."""
    parts = line.split(maxsplit=1)
    cmd, rest = parts[0].lower(), (parts[1] if len(parts) > 1 else "")

    if cmd in ("/exit", "/quit", "/q"):
        return True

    if cmd == "/gate":
        if rest in GATE_HELP:
            atlas.gate.mode = rest
            atlas.gate.session_allow.clear()
            console.print(f"[green]gate → {rest}[/] ({GATE_HELP[rest]})")
        else:
            console.print(f"gate is [bold]{atlas.gate.mode}[/]. options: " + ", ".join(GATE_HELP))
    elif cmd == "/model":
        if rest:
            await client.set_model(rest)
            atlas.cfg.model = rest
            console.print(f"[green]model → {rest}[/]")
        else:
            console.print(f"model is [bold]{atlas.cfg.model}[/]")
    elif cmd == "/memory":
        facts = atlas.memory.recall(rest, 40)
        if not facts:
            console.print("[dim]nothing stored[/]")
        else:
            table = Table(box=None, pad_edge=False)
            table.add_column("key", style="cyan")
            table.add_column("value")
            for fact in facts:
                table.add_row(fact.key, fact.value[:100])
            console.print(table)
    elif cmd == "/audit":
        limit = int(rest) if rest.isdigit() else 20
        table = Table(box=None, pad_edge=False)
        table.add_column("when", style="dim")
        table.add_column("tool", style="cyan")
        table.add_column("decision")
        table.add_column("detail")
        for row in atlas.memory.events(limit):
            table.add_row(
                datetime.fromtimestamp(row["ts"]).strftime("%m-%d %H:%M:%S"),
                row["tool"] or "",
                row["decision"] or "",
                (row["detail"] or "")[:80],
            )
        console.print(table)
    elif cmd == "/sessions":
        from claude_agent_sdk import list_sessions

        for session in list_sessions(directory=atlas.cfg.workdir, limit=15):
            when = datetime.fromtimestamp(session.last_modified / 1000).strftime("%m-%d %H:%M")
            console.print(f"[dim]{when}[/] [cyan]{session.session_id}[/] {session.summary}")
    elif cmd == "/mcp":
        status = await client.get_mcp_status()
        console.print(status)
    elif cmd == "/new":
        console.print("[yellow]start a fresh process for a new session (exit, then `atlas`)[/]")
    else:
        console.print(f"[red]unknown command {cmd}[/]")
    return False


async def cmd_ask(cfg: Config, args) -> int:
    atlas = Atlas(cfg, interactive=sys.stdin.isatty())
    prompt = " ".join(args.prompt)
    if not sys.stdin.isatty():
        prompt = f"{prompt}\n\n--- stdin ---\n{sys.stdin.read()}"
    try:
        result = await atlas.drain(
            query(prompt=prompt, options=atlas.options(continue_conversation=args.continue_last)),
            show_tools=not args.quiet,
        )
        if not args.quiet:
            _print_result_footer(result)
    finally:
        atlas.close()
    return 0


async def cmd_run(cfg: Config, args) -> int:
    """Run one configured task now."""
    tasks = {t.name: t for t in cfg.tasks}
    if args.task not in tasks:
        console.print(f"[red]no task named {args.task}[/]. configured: {', '.join(tasks) or '(none)'}")
        return 1
    await run_task(cfg, tasks[args.task])
    return 0


async def run_task(cfg: Config, task: Task) -> None:
    atlas = Atlas(cfg, interactive=False)
    if task.gate:
        atlas.gate.mode = task.gate
    console.print(f"[dim]{datetime.now():%H:%M:%S}[/] [bold]task {task.name}[/]")
    try:
        result = await atlas.drain(query(prompt=task.prompt, options=atlas.options()))
        atlas.memory.log("task", task.name, task.prompt, decision="ran")
        _print_result_footer(result)
    except Exception as exc:
        console.print(f"[red]task {task.name} failed:[/] {exc}")
        atlas.memory.log("task", task.name, str(exc), decision="failed")
    finally:
        atlas.close()


async def cmd_daemon(cfg: Config, args) -> int:
    """Run every enabled task on its interval, forever."""
    tasks = [t for t in cfg.tasks if t.enabled]
    if not tasks:
        console.print("[yellow]no tasks configured[/] — add a [tasks.name] block to your config")
        return 1
    console.print(f"[bold]daemon[/] running {len(tasks)} task(s):")
    for task in tasks:
        console.print(f"  [cyan]{task.name}[/] every {task.every}")

    next_run = {t.name: 0.0 for t in tasks}
    while True:
        now = time.time()
        for task in tasks:
            if now >= next_run[task.name]:
                await run_task(cfg, task)
                next_run[task.name] = time.time() + parse_interval(task.every)
        await asyncio.sleep(min(30.0, max(1.0, min(next_run.values()) - time.time())))


def cmd_memory(cfg: Config, args) -> int:
    memory = Memory(cfg.db_path)
    if args.forget:
        console.print("deleted" if memory.forget(args.forget) else "no such key")
    elif args.set:
        key, _, value = args.set.partition("=")
        if not value:
            console.print("[red]use --set key=value[/]")
            return 1
        memory.remember(key.strip(), value.strip())
        console.print(f"remembered {key.strip()}")
    else:
        for fact in memory.recall(args.query or "", args.limit):
            console.print(f"[cyan]{fact.key}[/]: {fact.value}")
    memory.close()
    return 0


def cmd_init(cfg: Config, args) -> int:
    cfg.home.mkdir(parents=True, exist_ok=True)
    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    if cfg.config_path.exists():
        console.print(f"[yellow]{cfg.config_path} already exists[/]")
    else:
        cfg.config_path.write_text(example.read_text())
        console.print(f"wrote [green]{cfg.config_path}[/]")
    if not cfg.secrets_path.exists():
        cfg.secrets_path.write_text(
            '# Credentials Atlas can use. Reference them as {{secret:NAME}} in the\n'
            '# http tool so the value never enters the conversation.\n'
            '# github_token = "ghp_..."\n'
        )
        cfg.secrets_path.chmod(0o600)
        console.print(f"wrote [green]{cfg.secrets_path}[/] (chmod 600)")
    Memory(cfg.db_path).close()
    console.print(f"memory at [green]{cfg.db_path}[/]")
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas", description="Your assistant.")
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--gate", choices=list(GATE_HELP), help="override the approval gate")
    parser.add_argument("--model", help="override the model")
    parser.add_argument("--workdir", help="override the working directory")
    # Bare `atlas` means `atlas chat`, so the chat flags must always exist.
    parser.set_defaults(resume=None, continue_last=False)
    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser("chat", help="interactive session (default)")
    chat.add_argument("--resume", help="resume a session id")
    chat.add_argument("-c", "--continue-last", action="store_true")

    ask = sub.add_parser("ask", help="one-shot question; reads stdin when piped")
    ask.add_argument("prompt", nargs="+")
    ask.add_argument("-c", "--continue-last", action="store_true")
    ask.add_argument("-q", "--quiet", action="store_true", help="answer only")

    run = sub.add_parser("run", help="run one configured task now")
    run.add_argument("task")

    sub.add_parser("daemon", help="run configured tasks on their schedules")

    memory = sub.add_parser("memory", help="inspect or edit stored memory")
    memory.add_argument("query", nargs="?")
    memory.add_argument("--limit", type=int, default=40)
    memory.add_argument("--set", metavar="KEY=VALUE")
    memory.add_argument("--forget", metavar="KEY")

    sub.add_parser("init", help="create ~/.atlas with a starter config")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        console.print(f"[red]config error:[/] {exc}")
        return 1
    if args.gate:
        cfg.gate = args.gate
    if args.model:
        cfg.model = args.model
    if args.workdir:
        cfg.workdir = args.workdir

    command = args.command or "chat"
    if command == "memory":
        return cmd_memory(cfg, args)
    if command == "init":
        return cmd_init(cfg, args)

    handlers = {"chat": cmd_chat, "ask": cmd_ask, "run": cmd_run, "daemon": cmd_daemon}
    try:
        return asyncio.run(handlers[command](cfg, args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
