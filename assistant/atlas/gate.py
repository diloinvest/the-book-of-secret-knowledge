"""The approval gate.

Design rule, and the whole point of this assistant: the gate never decides that
something is forbidden. It only decides whether to *ask you first*. Every "no"
in this file comes from a human typing it at the prompt.

Three modes:
  open    -- nothing is ever asked. Full speed, no seatbelt.
  danger  -- asks only for calls matching the danger patterns in config
             (destructive shell commands, money movement, outbound sends).
  strict  -- asks for anything that is not plainly read-only.

Mechanically: the session runs in `bypassPermissions` so the SDK adds no prompts
of its own, and a `PreToolUse` hook -- which runs before every other permission
step -- escalates the calls the gate wants confirmed to `ask`, which routes them
to the `can_use_tool` callback below.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from .config import Config
from .memory import Memory

# Asked for even in "open" mode, because no mode auto-approves them anyway:
# the SDK always routes these to the callback.
ALWAYS_INTERACTIVE = {"AskUserQuestion"}


class Gate:
    def __init__(
        self,
        cfg: Config,
        memory: Memory,
        prompt: Callable[[str, dict[str, Any], str], Any] | None = None,
    ):
        self.cfg = cfg
        self.memory = memory
        self.mode = cfg.gate
        self.prompt = prompt  # async callable returning "yes" | "always" | "no" | str reason
        self.session_allow: set[str] = set()
        self._danger = [re.compile(p, re.I) for p in cfg.danger_patterns]
        self._danger_tools = [re.compile(p, re.I) for p in cfg.danger_tool_patterns]
        self._readonly_tools = set(cfg.readonly_tools)
        self._readonly_pat = [re.compile(p, re.I) for p in cfg.readonly_tool_patterns]

    # --- classification -------------------------------------------------
    @staticmethod
    def _render(tool_input: dict[str, Any]) -> str:
        return json.dumps(tool_input, ensure_ascii=False, default=str)

    def is_readonly(self, tool: str) -> bool:
        return tool in self._readonly_tools or any(p.search(tool) for p in self._readonly_pat)

    @staticmethod
    def _bare_name(tool: str) -> str:
        """mcp__Coinbase__coinbase_transfer -> coinbase_transfer; Bash -> Bash."""
        return tool.rsplit("__", 1)[-1]

    def danger_reason(self, tool: str, tool_input: dict[str, Any]) -> str | None:
        """Why this call would be worth a second look, or None."""
        if tool.startswith("mcp__"):
            bare = self._bare_name(tool)
            for pat in self._danger_tools:
                if pat.search(bare):
                    return f"tool name matches {pat.pattern}"
        text = self._render(tool_input)
        for pat in self._danger:
            if pat.search(text):
                return f"input matches {pat.pattern}"
        return None

    def needs_confirmation(self, tool: str, tool_input: dict[str, Any]) -> str | None:
        if tool in self.session_allow:
            return None
        if self.mode == "open":
            return None
        if self.mode == "strict" and not self.is_readonly(tool):
            return "strict mode: not a read-only tool"
        return self.danger_reason(tool, tool_input)

    # --- SDK integration -------------------------------------------------
    async def pre_tool_use(self, input_data, tool_use_id, context) -> dict[str, Any]:
        """Runs before every tool call, ahead of every other permission step."""
        tool = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {}) or {}
        reason = self.needs_confirmation(tool, tool_input)

        if self.cfg.audit_log:
            self.memory.log(
                kind="tool",
                tool=tool,
                detail=tool_input,
                decision="ask" if reason else "auto",
                session=input_data.get("session_id"),
            )

        # The hook is the single authority. Returning {} would only mean
        # "keep evaluating", and with no allow rules the call would fall through
        # to can_use_tool regardless -- so decide here, explicitly: "allow" for
        # calls the gate clears, "ask" for the ones it wants confirmed (which
        # routes them to can_use_tool). This is what makes "open" mode truly
        # run everything with zero prompts.
        decision = "ask" if reason else "allow"
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason or "allowed by gate",
            }
        }

    async def can_use_tool(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Ask the human. Anything that reaches here was escalated on purpose."""
        if self.prompt is None:
            # Unattended run (scheduler, pipe): nobody is here to answer, so the
            # configured gate decides. In "open" this is never reached.
            self.memory.log("gate", tool_name, input_data, decision="denied-unattended")
            return PermissionResultDeny(
                message=(
                    "No one is at the terminal to confirm this. Re-run the task with "
                    "gate = \"open\" if it should proceed unattended."
                )
            )

        reason = context.decision_reason or self.needs_confirmation(tool_name, input_data) or "confirm"
        answer = await self.prompt(tool_name, input_data, reason)

        if answer == "always":
            self.session_allow.add(tool_name)
            self.memory.log("gate", tool_name, input_data, decision="always")
            return PermissionResultAllow(updated_input=input_data)
        if answer == "yes":
            self.memory.log("gate", tool_name, input_data, decision="allowed")
            return PermissionResultAllow(updated_input=input_data)
        if answer == "no":
            self.memory.log("gate", tool_name, input_data, decision="denied")
            return PermissionResultDeny(message="Declined by the user.")

        # Anything else is free-text steering: deny this call, tell Claude why.
        self.memory.log("gate", tool_name, input_data, decision=f"denied: {answer}")
        return PermissionResultDeny(message=str(answer))


def build_hooks(gate: Gate) -> dict[str, Any]:
    from claude_agent_sdk import HookMatcher

    # No matcher: this fires for every tool, built-in and MCP alike.
    return {"PreToolUse": [HookMatcher(hooks=[gate.pre_tool_use])]}
