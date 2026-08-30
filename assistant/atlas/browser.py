"""A real browser, for the parts of the internet that plain HTTP can't reach.

Chromium driven through Playwright, with a persistent profile on disk so that
logins, cookies and sessions survive across Atlas runs -- once you sign in to a
site, Atlas stays signed in. The browser launches lazily on first use and is
shared across every browser tool call in a session.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Any

_CHROME_PATTERNS = [
    "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
    "/opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell",
]


def find_chrome() -> str | None:
    """Locate a Chromium binary. Env override wins; otherwise let Playwright
    use its own managed download by returning None."""
    override = os.environ.get("ATLAS_CHROME")
    if override and os.path.exists(override):
        return override
    for pattern in _CHROME_PATTERNS:
        for match in sorted(glob.glob(pattern), reverse=True):
            if os.path.exists(match):
                return match
    return None


class Browser:
    """Lazily-started, session-shared Chromium with a persistent profile."""

    def __init__(self, profile_dir: Path, headless: bool = True):
        self.profile_dir = profile_dir
        self.headless = headless
        self._pw = None
        self._context = None
        self._page = None

    async def _ensure(self):
        if self._context is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # surfaced to the model as a tool error
            raise RuntimeError(
                "The browser tools need Playwright. Install it with "
                "`pip install playwright` and, on your own machine, "
                "`playwright install chromium`."
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        exe = find_chrome()
        kwargs: dict[str, Any] = {"headless": self.headless, "args": launch_args}
        if exe:
            kwargs["executable_path"] = exe
        # A persistent context keeps cookies/localStorage between sessions.
        self._context = await self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), **kwargs
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

    async def page(self):
        await self._ensure()
        return self._page

    async def goto(self, url: str, wait: str = "load", timeout: float = 45000) -> dict[str, Any]:
        page = await self.page()
        resp = await page.goto(url, wait_until=wait, timeout=timeout)
        return {
            "url": page.url,
            "status": resp.status if resp else None,
            "title": await page.title(),
        }

    async def text(self, max_chars: int = 20000) -> str:
        page = await self.page()
        body = await page.inner_text("body")
        if len(body) > max_chars:
            body = body[:max_chars] + f"\n... [truncated, {len(body)} chars]"
        return body

    async def html(self, max_chars: int = 40000) -> str:
        page = await self.page()
        content = await page.content()
        return content[:max_chars]

    async def click(self, selector: str, timeout: float = 15000) -> str:
        page = await self.page()
        await page.click(selector, timeout=timeout)
        return f"clicked {selector}; now at {page.url}"

    async def fill(self, selector: str, value: str, timeout: float = 15000) -> str:
        page = await self.page()
        await page.fill(selector, value, timeout=timeout)
        return f"filled {selector}"

    async def press(self, selector: str, key: str, timeout: float = 15000) -> str:
        page = await self.page()
        await page.press(selector, key, timeout=timeout)
        return f"pressed {key} on {selector}; now at {page.url}"

    async def eval_js(self, script: str) -> Any:
        page = await self.page()
        return await page.evaluate(script)

    async def screenshot(self, path: str, full_page: bool = True) -> str:
        page = await self.page()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=path, full_page=full_page)
        return path

    async def current_url(self) -> str:
        page = await self.page()
        return page.url

    async def close(self):
        import asyncio
        try:
            if self._context is not None:
                await asyncio.wait_for(self._context.close(), timeout=8)
        except Exception:
            pass
        finally:
            self._context = None
        try:
            if self._pw is not None:
                await asyncio.wait_for(self._pw.stop(), timeout=8)
        except Exception:
            pass
        finally:
            self._pw = None

    @property
    def started(self) -> bool:
        return self._context is not None
