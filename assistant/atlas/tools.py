"""Atlas' own tools, running in-process alongside the built-in Claude Code set.

The built-in tools already cover files, shell, search and the web. What is added
here is the rest of what a personal assistant needs: durable memory, raw HTTP to
any endpoint, credential injection that keeps secrets out of the transcript,
scratch code execution, and desktop notifications.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Any

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

from .browser import Browser
from .config import Config
from .memory import Memory

SECRET_RE = "{{secret:NAME}}"


def _text(body: str, is_error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": body}]}
    if is_error:
        out["is_error"] = True
    return out


def _expand_secrets(value: Any, secrets: dict[str, str]) -> Any:
    """Replace {{secret:NAME}} placeholders just before the request goes out."""
    if isinstance(value, str):
        for name, secret in secrets.items():
            value = value.replace("{{secret:" + name + "}}", secret)
        return value
    if isinstance(value, dict):
        return {k: _expand_secrets(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_secrets(v, secrets) for v in value]
    return value


def make_tools(cfg: Config, memory: Memory, secrets: dict[str, str]) -> list:
    """Build Atlas' tool set. Returned as a list so it can be tested directly."""

    browser = Browser(cfg.home / "browser", headless=cfg.browser_headless)

    # --- memory ---------------------------------------------------------
    @tool(
        "remember",
        "Store a durable fact about the user, their systems, preferences or "
        "ongoing work. Survives across sessions and is loaded into your context "
        "at the start of every conversation. Use a stable, specific key.",
        {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Stable identifier, e.g. 'deploy.staging.host'"},
                "value": {"type": "string"},
                "tags": {"type": "string", "description": "Optional comma-separated tags"},
            },
            "required": ["key", "value"],
        },
    )
    async def remember(args: dict[str, Any]) -> dict[str, Any]:
        memory.remember(args["key"], args["value"], args.get("tags", ""))
        return _text(f"Remembered {args['key']}.")

    @tool(
        "recall",
        "Search stored memory. Empty query returns the most recently updated facts.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
    )
    async def recall(args: dict[str, Any]) -> dict[str, Any]:
        facts = memory.recall(args.get("query", ""), int(args.get("limit", 20)))
        if not facts:
            return _text("Nothing stored matches that.")
        return _text("\n".join(f"{f.key}: {f.value}" + (f"  [{f.tags}]" if f.tags else "") for f in facts))

    @tool(
        "forget",
        "Delete a stored fact by its exact key.",
        {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
    )
    async def forget(args: dict[str, Any]) -> dict[str, Any]:
        ok = memory.forget(args["key"])
        return _text(f"Deleted {args['key']}." if ok else f"No fact stored under {args['key']}.")

    # --- network --------------------------------------------------------
    @tool(
        "http",
        "Make an arbitrary HTTP request to any URL, with any method, headers and "
        "body. Unlike WebFetch this reaches private hosts, APIs behind tokens and "
        "non-HTML endpoints, and returns the raw response. To authenticate without "
        "putting the credential in the conversation, write " + SECRET_RE + " in a "
        "header or the body; it is substituted at send time. Call secret_list to "
        "see which names exist.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "default": "GET"},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "body": {"type": "string", "description": "Raw request body; JSON should be pre-serialized"},
                "params": {"type": "object", "additionalProperties": {"type": "string"}},
                "timeout": {"type": "number", "default": 60},
                "max_chars": {"type": "integer", "default": 20000},
            },
            "required": ["url"],
        },
    )
    async def http(args: dict[str, Any]) -> dict[str, Any]:
        url = _expand_secrets(args["url"], secrets)
        headers = _expand_secrets(args.get("headers") or {}, secrets)
        body = _expand_secrets(args.get("body"), secrets)
        params = _expand_secrets(args.get("params") or {}, secrets)
        method = str(args.get("method", "GET")).upper()
        limit = int(args.get("max_chars", 20000))

        try:
            async with httpx.AsyncClient(
                timeout=float(args.get("timeout", 60)), follow_redirects=True
            ) as client:
                resp = await client.request(
                    method, url, headers=headers, content=body, params=params or None
                )
        except Exception as exc:  # network failures are results, not crashes
            return _text(f"{type(exc).__name__}: {exc}", is_error=True)

        text = resp.text
        truncated = len(text) > limit
        head = json.dumps(dict(resp.headers), ensure_ascii=False, indent=2)
        return _text(
            f"HTTP {resp.status_code} {resp.reason_phrase}\n{head}\n\n"
            f"{text[:limit]}{f'... [truncated, {len(text)} chars total]' if truncated else ''}"
        )

    # --- credentials ----------------------------------------------------
    @tool(
        "secret_list",
        "List the names of credentials available for " + SECRET_RE + " substitution. "
        "Returns names only, never values.",
        {"type": "object", "properties": {}},
    )
    async def secret_list(args: dict[str, Any]) -> dict[str, Any]:
        if not secrets:
            return _text(f"No secrets configured. Add them to {cfg.secrets_path}.")
        return _text("Available secret names:\n" + "\n".join(sorted(secrets)))

    @tool(
        "secret_get",
        "Reveal a credential's actual value. Prefer " + SECRET_RE + " placeholders "
        "in the http tool instead; use this only when a value must be pasted into "
        "a command or file, since it puts the secret into the transcript.",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    async def secret_get(args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"]
        if name not in secrets:
            return _text(f"No secret named {name}.", is_error=True)
        memory.log("secret", "secret_get", {"name": name}, decision="revealed")
        return _text(secrets[name])

    # --- full browser ---------------------------------------------------
    @tool(
        "browse",
        "Open a URL in a real Chromium browser and return the fully rendered "
        "page text. Use this for anything plain HTTP can't read: JavaScript-heavy "
        "sites, single-page apps, and pages behind a login you have already signed "
        "into. The browser keeps its cookies and sessions between runs, so once you "
        "log in to a site it stays logged in.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "wait": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                    "default": "load",
                },
                "max_chars": {"type": "integer", "default": 20000},
            },
            "required": ["url"],
        },
    )
    async def browse(args: dict[str, Any]) -> dict[str, Any]:
        try:
            meta = await browser.goto(
                _expand_secrets(args["url"], secrets), wait=args.get("wait", "load")
            )
            text = await browser.text(int(args.get("max_chars", 20000)))
        except Exception as exc:
            return _text(f"{type(exc).__name__}: {exc}", is_error=True)
        return _text(f"{meta['status']} {meta['title']}\n{meta['url']}\n\n{text}")

    @tool(
        "browser",
        "Interact with the page currently open in the browser: click, fill in a "
        "field, press a key, run JavaScript, screenshot, or read the URL/HTML. "
        "Selectors are CSS or Playwright text= selectors. Use this to work through "
        "logins, forms, search boxes and multi-step flows.",
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "fill", "press", "eval", "screenshot", "html", "url", "text"],
                },
                "selector": {"type": "string"},
                "value": {"type": "string", "description": "text for fill, key for press"},
                "script": {"type": "string", "description": "JavaScript for eval"},
                "path": {"type": "string", "description": "output path for screenshot"},
            },
            "required": ["action"],
        },
    )
    async def browser_act(args: dict[str, Any]) -> dict[str, Any]:
        action = args["action"]
        try:
            if action == "click":
                return _text(await browser.click(args["selector"]))
            if action == "fill":
                return _text(await browser.fill(args["selector"], _expand_secrets(args.get("value", ""), secrets)))
            if action == "press":
                return _text(await browser.press(args["selector"], args.get("value", "Enter")))
            if action == "eval":
                return _text(json.dumps(await browser.eval_js(args["script"]), ensure_ascii=False, default=str)[:8000])
            if action == "screenshot":
                path = args.get("path") or str(cfg.home / "screenshot.png")
                return _text(f"saved {await browser.screenshot(path)}")
            if action == "html":
                return _text(await browser.html())
            if action == "url":
                return _text(await browser.current_url())
            if action == "text":
                return _text(await browser.text())
            return _text(f"unknown action {action}", is_error=True)
        except Exception as exc:
            return _text(f"{type(exc).__name__}: {exc}", is_error=True)

    @tool(
        "download",
        "Download any URL straight to a file on disk -- binaries, archives, media, "
        "anything, not just text. Streams so large files are fine. Supports "
        "{{secret:NAME}} in the URL and headers.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "path": {"type": "string", "description": "destination file path"},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "timeout": {"type": "number", "default": 300},
            },
            "required": ["url", "path"],
        },
    )
    async def download(args: dict[str, Any]) -> dict[str, Any]:
        url = _expand_secrets(args["url"], secrets)
        headers = _expand_secrets(args.get("headers") or {}, secrets)
        dest = Path(args["path"]).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        try:
            async with httpx.AsyncClient(
                timeout=float(args.get("timeout", 300)), follow_redirects=True
            ) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code >= 400:
                        return _text(f"HTTP {resp.status_code} for {url}", is_error=True)
                    with dest.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(65536):
                            fh.write(chunk)
                            total += len(chunk)
        except Exception as exc:
            return _text(f"{type(exc).__name__}: {exc}", is_error=True)
        return _text(f"downloaded {total} bytes to {dest}")

    # --- compute --------------------------------------------------------
    @tool(
        "python",
        "Run Python in a fresh subprocess and return stdout/stderr. For quick "
        "computation, parsing and one-off scripting where writing a file would be "
        "overkill. Full standard library and whatever is installed.",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "timeout": {"type": "number", "default": 120},
            },
            "required": ["code"],
        },
    )
    async def python_exec(args: dict[str, Any]) -> dict[str, Any]:
        started = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", args["code"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cfg.workdir,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=float(args.get("timeout", 120))
            )
        except asyncio.TimeoutError:
            proc.kill()
            return _text(f"Timed out after {args.get('timeout', 120)}s.", is_error=True)
        except Exception as exc:
            return _text(f"{type(exc).__name__}: {exc}", is_error=True)

        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        parts = [f"exit {proc.returncode} in {time.time() - started:.1f}s"]
        if out:
            parts.append(f"--- stdout ---\n{out}")
        if err:
            parts.append(f"--- stderr ---\n{err}")
        return _text("\n".join(parts), is_error=proc.returncode != 0)

    # --- desktop --------------------------------------------------------
    @tool(
        "notify",
        "Send a desktop notification. Use when you finish long-running work or "
        "need attention while the user is in another window.",
        {
            "type": "object",
            "properties": {"title": {"type": "string"}, "message": {"type": "string"}},
            "required": ["title", "message"],
        },
    )
    async def notify(args: dict[str, Any]) -> dict[str, Any]:
        title, message = args["title"], args["message"]
        if shutil.which("notify-send"):
            cmd = ["notify-send", title, message]
        elif sys.platform == "darwin":
            script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
            cmd = ["osascript", "-e", script]
        else:
            print(f"\n[{title}] {message}", flush=True)
            return _text("Printed to the terminal (no desktop notifier found).")
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=10)
        except Exception as exc:
            return _text(f"Notification failed: {exc}", is_error=True)
        return _text("Notification sent.")

    @tool(
        "system_info",
        "Report the host: OS, hostname, user, CPU count, load, disk and memory use, "
        "and where Atlas keeps its own state.",
        {"type": "object", "properties": {}},
    )
    async def system_info(args: dict[str, Any]) -> dict[str, Any]:
        usage = shutil.disk_usage(cfg.workdir)
        info = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "hostname": platform.node(),
            "user": getpass.getuser(),
            "cpu_count": os.cpu_count(),
            "cwd": cfg.workdir,
            "disk_free_gb": round(usage.free / 2**30, 1),
            "disk_total_gb": round(usage.total / 2**30, 1),
            "atlas_home": str(cfg.home),
            "memory_db": str(cfg.db_path),
            "gate": cfg.gate,
        }
        try:
            info["loadavg"] = os.getloadavg()
        except (OSError, AttributeError):
            pass
        return _text(json.dumps(info, ensure_ascii=False, indent=2))

    make_tools.last_browser = browser  # for optional cleanup by callers
    return [
        remember, recall, forget,
        http, download, browse, browser_act,
        secret_list, secret_get,
        python_exec, notify, system_info,
    ]


def build_server(cfg: Config, memory: Memory, secrets: dict[str, str]):
    """Create the in-process MCP server exposing Atlas' own tools.

    The shared Browser instance is attached to the returned server object as
    ``.browser`` so the caller can close it when the session ends.
    """
    tools = make_tools(cfg, memory, secrets)
    return create_sdk_mcp_server(name="atlas", version="0.1.0", tools=tools)
