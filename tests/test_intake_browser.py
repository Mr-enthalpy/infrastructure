"""Real-browser intake test (goal AC-26; requires optional Playwright).

Run in the disposable D2 environment:

    python -m pip install -e ".[browser]"
    python -m playwright install chromium
    python -m pytest tests/test_intake_browser.py -q

The module is skipped when Playwright or its browser binary is unavailable.
A skip is not acceptance evidence; capture the screenshot/output from a real
run before marking AC-26 PASS.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from aiohttp import web

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from citespec_resource.enrollment import server as intake_server  # noqa: E402
from citespec_resource.enrollment import store as intake_store  # noqa: E402

ONBOARDING = {
    "guide_url": "https://lab.example.invalid/compute",
    "apply_url": "https://lab.example.invalid/compute/join",
    "support_label": "计算资源管理员",
    "support_contact": "admin@lab.example.invalid",
}
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test@lab"


class _Server:
    def __init__(self, app: web.Application) -> None:
        self.loop = asyncio.new_event_loop()
        self.runner = web.AppRunner(app)
        self.port = 0
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.runner.setup())
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        self.loop.run_until_complete(site.start())
        assert site._server is not None  # aiohttp internal; stable in test scope
        self.port = int(site._server.sockets[0].getsockname()[1])
        self.ready.set()
        self.loop.run_forever()

    def start(self) -> str:
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("intake test server did not start")
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        future = asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop)
        future.result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


@pytest.fixture
def browser_url(tmp_path: Path):
    db = tmp_path / "intake.db"
    connection = intake_store.init_intake_db(db)
    connection.close()
    app = intake_server.create_app(requests_db=str(db), onboarding=ONBOARDING)
    server = _Server(app)
    try:
        yield server.start(), str(db)
    finally:
        server.close()


def test_browser_submit_and_status(browser_url, tmp_path: Path):
    url, db = browser_url
    connection = intake_store.connect(db)
    invitation = intake_store.create_invitation(connection, created_by="browser-test")
    connection.close()

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Playwright Chromium is not installed: {exc}")
        try:
            page = browser.new_page()
            page.goto(url)
            page.fill("#invite_id", invitation["invite_id"])
            page.fill("#invite_secret", invitation["invite_secret"])
            page.fill("#display_name", "Browser Member")
            page.fill("#username", "browsermember")
            page.fill("#affiliation", "Browser Lab")
            page.fill("#contact", "browser@example.invalid")
            page.fill("#purpose", "browser acceptance test")
            page.fill("#public_keys", PUBLIC_KEY)
            page.check("#agree_terms")
            page.click("#submit-button")
            page.wait_for_selector("#submit-result:not([hidden])")
            result = page.inner_text("#submit-result")
            assert "申请编号" in result
            assert "回执" in result
            # The receipt must not leak into the address bar/history.
            assert "receipt" not in page.url.lower()
            page.screenshot(path=str(tmp_path / "intake-submitted.png"), full_page=True)

            # Read the one-time values from the rendered receipt. They are
            # never placed in a URL by the page.
            secrets = page.locator("#submit-result .secret")
            request_id = secrets.nth(0).inner_text().strip()
            receipt = secrets.nth(1).inner_text().strip()
            page.fill("#status_request_id", request_id)
            page.fill("#status_receipt", receipt)
            page.click("#status-form button[type=submit]")
            page.wait_for_selector("#status-result:not([hidden])")
            assert "待审核" in page.inner_text("#status-result")
            page.screenshot(path=str(tmp_path / "intake-status.png"), full_page=True)
        finally:
            browser.close()
