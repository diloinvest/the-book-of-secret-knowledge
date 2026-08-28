"""Configuration loading for Atlas.

Everything the assistant is allowed to reach is described here. The defaults are
deliberately wide open: the only thing that ever stops a tool call is the
approval gate, and the gate only ever *asks* -- it never decides on its own that
something is off limits.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - only on 3.10
    import tomli as tomllib


def atlas_home() -> Path:
    return Path(os.environ.get("ATLAS_HOME", Path.home() / ".atlas")).expanduser()


# Commands and tool calls that get a confirmation prompt in "danger" gate mode.
# These are regexes matched case-insensitively against the rendered tool input.
DEFAULT_DANGER_PATTERNS: list[str] = [
    r"\brm\s+(-\w*\s+)*-\w*[rf]",
    r"\brmdir\b",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+if=",
    r">\s*/dev/(sd|nvme|disk)",
    r"\bshred\b",
    r"\bfdisk\b",
    r"\bparted\b",
    r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b",
    r"\bchmod\s+-R\s+777\b",
    r"\bchown\s+-R\b",
    r"\bgit\s+push\b.*(--force|-f\b)",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-\w*[dfx]",
    r"\bdrop\s+(table|database|schema)\b",
    r"\btruncate\s+table\b",
    r"\bdelete\s+from\b",
    r"\bcurl\b[^|]*\|\s*(ba)?sh\b",
    r"\bwget\b[^|]*\|\s*(ba)?sh\b",
    r"\bsudo\b",
    r"\bsystemctl\s+(stop|disable|mask)\b",
    r"\bdocker\s+(rm|rmi|system\s+prune)\b",
    r"\bkubectl\s+delete\b",
    r"\bterraform\s+(destroy|apply)\b",
    r"\bnpm\s+publish\b|\bpypi\b.*upload|\btwine\s+upload\b",
    r"\.ssh/|\bid_rsa\b|\bid_ed25519\b",
    r"/etc/(passwd|shadow|sudoers)",
]

# MCP tools whose names imply an outward-facing or irreversible effect: sending
# mail, moving money, publishing, deleting remote records. These are matched
# against the tool's own name -- the part after the last "__" -- so a verb
# anywhere in it counts, whether the server separates words with _ or -.
DEFAULT_DANGER_TOOL_PATTERNS: list[str] = [
    r"(^|[-_])(send|forward|reply|email)([-_]|$)",
    r"(^|[-_])(delete|trash|remove|destroy|drop|revoke)([-_]|$)",
    r"(^|[-_])(transfer|withdraw|payment|refund|charge|order|orders)([-_]|$)",
    r"(^|[-_])(publish|deploy|merge|release|invite|share)([-_]|$)",
    r"(^|[-_])(convert_execute|close_position)([-_]|$)",
    r"(^|[-_])create[-_](pull_request|discount|invite|user)",
    r"(^|[-_])(apply_migration|execute_sql|run_secret_scanning)([-_]|$)",
]

# Tools that only read. In "strict" gate mode everything outside this set asks
# first; in the other modes the list is unused.
DEFAULT_READONLY_TOOLS: list[str] = [
    "Read", "Glob", "Grep", "WebSearch", "WebFetch", "NotebookRead",
    "TodoWrite", "Task", "BashOutput",
]
DEFAULT_READONLY_TOOL_PATTERNS: list[str] = [
    r"mcp__.*__(get|list|search|read|fetch|query|show|find|describe|check)",
    r"mcp__atlas__(recall|secret_list|system_info)",
]


@dataclass
class Task:
    """A prompt Atlas runs on its own schedule."""

    name: str
    prompt: str
    every: str = "1h"          # 30s / 15m / 2h / 1d
    gate: str | None = None    # override the gate for unattended runs
    enabled: bool = True


@dataclass
class Config:
    # --- model ---------------------------------------------------------
    model: str = "claude-opus-5"
    fallback_model: str | None = None
    effort: str | None = None            # low | medium | high | xhigh | max
    max_turns: int | None = None
    max_budget_usd: float | None = None

    # --- what it can touch ---------------------------------------------
    workdir: str = str(Path.home())
    additional_directories: list[str] = field(default_factory=list)
    # Loaded from ~/.claude and ./.claude so skills, commands, CLAUDE.md and
    # .mcp.json you already have keep working.
    setting_sources: list[str] = field(default_factory=lambda: ["user", "project", "local"])
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    # Tools Atlas may never call. Empty by default -- this is the one knob that
    # actually removes capability, and it is yours to set.
    disallowed_tools: list[str] = field(default_factory=list)
    skills: Any = "all"                  # "all", or a list of skill names
    # "default" routes every call through the gate. "bypassPermissions" skips the
    # gate entirely for maximum speed -- nothing will ever ask.
    permission_mode: str = "default"

    # --- approval gate --------------------------------------------------
    gate: str = "danger"  # open | danger | strict
    danger_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_DANGER_PATTERNS))
    danger_tool_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_DANGER_TOOL_PATTERNS))
    readonly_tools: list[str] = field(default_factory=lambda: list(DEFAULT_READONLY_TOOLS))
    readonly_tool_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_READONLY_TOOL_PATTERNS))

    # --- personality & memory ------------------------------------------
    persona: str = ""
    memory_context_items: int = 25
    audit_log: bool = True

    # --- autonomy -------------------------------------------------------
    tasks: list[Task] = field(default_factory=list)

    # --- paths ----------------------------------------------------------
    home: Path = field(default_factory=atlas_home)

    @property
    def db_path(self) -> Path:
        return self.home / "memory.db"

    @property
    def secrets_path(self) -> Path:
        return self.home / "secrets.toml"

    @property
    def config_path(self) -> Path:
        return self.home / "config.toml"


def load_config(path: Path | None = None) -> Config:
    """Read ~/.atlas/config.toml (or ATLAS_CONFIG) over the defaults."""
    home = atlas_home()
    if path is None:
        env = os.environ.get("ATLAS_CONFIG")
        path = Path(env).expanduser() if env else home / "config.toml"

    cfg = Config(home=home)
    if not path.exists():
        return cfg

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    for key in (
        "model", "fallback_model", "effort", "max_turns", "max_budget_usd",
        "workdir", "additional_directories", "setting_sources", "disallowed_tools",
        "skills", "permission_mode", "gate", "persona", "memory_context_items",
        "audit_log",
    ):
        if key in raw:
            setattr(cfg, key, raw[key])

    # Danger/read-only lists extend the defaults unless the file says "replace".
    for key, default in (
        ("danger_patterns", DEFAULT_DANGER_PATTERNS),
        ("danger_tool_patterns", DEFAULT_DANGER_TOOL_PATTERNS),
        ("readonly_tools", DEFAULT_READONLY_TOOLS),
        ("readonly_tool_patterns", DEFAULT_READONLY_TOOL_PATTERNS),
    ):
        if key in raw:
            setattr(cfg, key, list(raw[key]))
        elif f"extra_{key}" in raw:
            setattr(cfg, key, list(default) + list(raw[f"extra_{key}"]))

    cfg.mcp_servers = dict(raw.get("mcp_servers", {}))
    cfg.tasks = [
        Task(
            name=name,
            prompt=body["prompt"],
            every=body.get("every", "1h"),
            gate=body.get("gate"),
            enabled=body.get("enabled", True),
        )
        for name, body in raw.get("tasks", {}).items()
    ]

    cfg.workdir = str(Path(cfg.workdir).expanduser())
    cfg.additional_directories = [str(Path(p).expanduser()) for p in cfg.additional_directories]

    if cfg.gate not in {"open", "danger", "strict"}:
        raise ValueError(f"gate must be open, danger or strict (got {cfg.gate!r})")
    return cfg


def load_secrets(cfg: Config) -> dict[str, str]:
    """Credentials Atlas can use without them ever entering the transcript."""
    if not cfg.secrets_path.exists():
        return {}
    with cfg.secrets_path.open("rb") as fh:
        return {k: str(v) for k, v in tomllib.load(fh).items()}


def parse_interval(spec: str) -> float:
    """'30s' / '15m' / '2h' / '1d' -> seconds."""
    spec = str(spec).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if spec and spec[-1] in units:
        return float(spec[:-1]) * units[spec[-1]]
    return float(spec)
