"""Assembles the agent: options, tools, memory, gate."""

from __future__ import annotations

from typing import Any, Callable

from claude_agent_sdk import ClaudeAgentOptions

from .config import Config, load_secrets
from .gate import Gate, build_hooks
from .memory import Memory
from .tools import build_server

BASE_PERSONA = """\
You are Atlas, USERNAME's personal assistant. You run on their own machine with
their own credentials, at their request.

How you work:
- You have real access: the shell, the whole filesystem, the network, and every
  MCP service configured below. Use it. When a question can be answered by
  looking, look, rather than telling the user how they could look.
- The user is the one who decides what is worth doing and what is risky. If an
  action needs their confirmation, they will get a prompt; you do not need to
  ask for permission in prose first, and you do not need to warn them about
  ordinary operations on their own systems.
- Say what you actually did and what you found, including when something failed.
  Never claim a step succeeded that you did not verify.
- Prefer the specific tool over a shell equivalent: `http` for APIs, `python`
  for computation, the MCP tools for the services they belong to.
- Remember what matters. When you learn something durable about the user, their
  machines, their projects or their preferences, store it with `remember`. Do
  not re-ask for things you could recall.
- Credentials live in a secrets file. Use {{secret:NAME}} placeholders in `http`
  so they never enter the conversation; only use `secret_get` when a value has
  to be typed somewhere.
"""


def build_options(
    cfg: Config,
    memory: Memory,
    gate: Gate,
    *,
    resume: str | None = None,
    continue_conversation: bool = False,
    extra_prompt: str = "",
) -> ClaudeAgentOptions:
    import getpass

    secrets = load_secrets(cfg)
    from .tools import make_tools

    atlas_server = build_server(cfg, memory, secrets)
    # make_tools stashes the shared Browser it created; hand it to the gate so
    # the runtime can close it on shutdown.
    gate.browser = getattr(make_tools, "last_browser", None)

    mcp_servers: dict[str, Any] = {"atlas": atlas_server}
    mcp_servers.update(cfg.mcp_servers)

    prompt_parts = [BASE_PERSONA.replace("USERNAME", getpass.getuser())]
    if cfg.persona:
        prompt_parts.append(cfg.persona)
    remembered = memory.context_block(cfg.memory_context_items)
    if remembered:
        prompt_parts.append(remembered)
    if extra_prompt:
        prompt_parts.append(extra_prompt)

    options = ClaudeAgentOptions(
        model=cfg.model,
        fallback_model=cfg.fallback_model,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "\n\n".join(prompt_parts),
        },
        cwd=cfg.workdir,
        add_dirs=list(cfg.additional_directories),
        setting_sources=list(cfg.setting_sources),
        skills=cfg.skills,
        mcp_servers=mcp_servers,
        # Deliberately no allow rules: an entry like "mcp__gmail__*" would
        # auto-approve those tools *before* the gate is consulted, so the gate
        # could never ask about sending mail. With the list empty, every call
        # reaches can_use_tool and the gate stays the single authority. The
        # gate grants everything itself when its mode says so.
        allowed_tools=[],
        disallowed_tools=list(cfg.disallowed_tools),
        permission_mode=cfg.permission_mode,
        can_use_tool=gate.can_use_tool,
        hooks=build_hooks(gate),
        max_turns=cfg.max_turns,
        max_budget_usd=cfg.max_budget_usd,
        continue_conversation=continue_conversation,
        resume=resume,
    )
    if cfg.effort:
        options.effort = cfg.effort
    return options
